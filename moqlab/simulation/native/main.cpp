// SPDX-License-Identifier: GPL-2.0-only
#include <ns3/core-module.h>
#include <ns3/network-module.h>
#include <ns3/internet-module.h>
#include <ns3/bridge-module.h>
#include <ns3/traffic-control-module.h>
#include <ns3/applications-module.h>
#include <ns3/simple-net-device.h>
#include <ns3/simple-channel.h>
#include <ns3/dualpi2-queue-disc.h>
#include <nlohmann/json.hpp>
#include "sbd/ReceiveIntervals.h"
extern "C" {
#include "picotls.h"
#include "picoquic.h"
#include "picoquic_internal.h"
#include "picoquic_unified_log.h"
#include "picoquic_qlog.h"
#include "tls_api.h"
#include "picoquic_crypto_provider_api.h"
}
#include <arpa/inet.h>
#include <fstream>
#include <filesystem>
#include <iomanip>
#include <map>
#include <set>
#include <deque>
#include <memory>
#include <numeric>
#include <sstream>

using namespace ns3;
using json = nlohmann::json;
namespace sbd = openmoq::moqx::sbd;
static uint64_t nowUs() { return Simulator::Now().GetMicroSeconds(); }
static void require(bool value, const std::string& message) { if (!value) throw std::runtime_error(message); }
static uint64_t simulationRandom=1;
// Reproducible protocol RNG for isolated simulation only; never use for network service keys.
static void simulationRandomBytes(void* data,size_t length) {picoquic_test_random_bytes(&simulationRandom,static_cast<uint8_t*>(data),length);}
static uint64_t sequence=0;
static json cfg;
static std::filesystem::path output;
static std::ofstream events, packets, queues, snapshots, truths, applications;
static void record(std::ofstream& file, json value) {
    value["sequence"] = ++sequence;value["time_us"] = nowUs(); file << value.dump() << '\n';
    require(bool(file), "output write failed");
}

class SourceMacTag : public Tag {
public:
    Mac48Address source;
    static TypeId GetTypeId() { static TypeId t=TypeId("MoqlabSourceMacTag").SetParent<Tag>().AddConstructor<SourceMacTag>();return t; }
    TypeId GetInstanceTypeId() const override {return GetTypeId();}
    uint32_t GetSerializedSize() const override {return 6;}
    void Serialize(TagBuffer b) const override {uint8_t data[6];source.CopyTo(data);b.Write(data,6);}
    void Deserialize(TagBuffer b) override {uint8_t data[6];b.Read(data,6);source.CopyFrom(data);}
    void Print(std::ostream& out) const override {out<<source;}
};
class BridgeItem : public QueueDiscItem {
public:
    BridgeItem(Ptr<Packet> p,const Address& a,uint16_t protocol):QueueDiscItem(p,a,protocol) {}
    void AddHeader() override {}
    bool GetUint8Value(Uint8Values field,uint8_t& value) const override {
        if(GetProtocol()!=0x800 || field!=IP_DSFIELD) return false;
        Ipv4Header h;GetPacket()->PeekHeader(h);value=h.GetTos();return true;
    }
    bool Mark() override {
        if(GetProtocol()!=0x800) return false;
        Ipv4Header h;GetPacket()->RemoveHeader(h);bool capable=h.GetEcn()!=Ipv4Header::ECN_NotECT;
        if(capable) h.SetEcn(Ipv4Header::ECN_CE);
        GetPacket()->AddHeader(h);return capable;
    }
    uint32_t Hash(uint32_t salt=0) const override {
        if(GetProtocol()!=0x800) return 0;
        auto p=GetPacket()->Copy();Ipv4Header h;p->RemoveHeader(h);
        return Create<Ipv4QueueDiscItem>(p,GetAddress(),GetProtocol(),h)->Hash(salt);
    }
};

static std::map<uint64_t,std::string> forwardFlowIds;
static std::map<std::string,std::set<std::string>> observedPaths;
static std::string identifyFlow(const Ipv4Header& header) {
    uint64_t key=(uint64_t(header.GetSource().Get())<<32)|header.GetDestination().Get();
    auto found=forwardFlowIds.find(key);return found==forwardFlowIds.end()?"":found->second;
}
static std::string identifyItem(Ptr<const QueueDiscItem> item) {
    if(auto ipv4=DynamicCast<const Ipv4QueueDiscItem>(item)) return identifyFlow(ipv4->GetHeader());
    if(item->GetProtocol()!=0x800) return "";
    Ipv4Header header;item->GetPacket()->PeekHeader(header);return identifyFlow(header);
}

// One in-service packet, no hidden waiting queue. TrafficControl owns backlog.
class WireDevice : public SimpleNetDevice {
public:
    static TypeId GetTypeId() {
        static TypeId id = TypeId("MoqlabWireDevice").SetParent<SimpleNetDevice>().AddConstructor<WireDevice>();
        return id;
    }
    WireDevice() {SetMtu(1500);}
    Ptr<WireDevice> peer;
    Ptr<NetDeviceQueueInterface> credit;
    Ptr<QueueDisc> disc;
    Ptr<UniformRandomVariable> random = CreateObject<UniformRandomVariable>();
    std::string key;
    double rate=1000, delay=0, jitter=0, loss=0;
    bool up=true;
    uint32_t overhead=38;
    uint64_t bytes=0, drops=0, marked=0, deviceDrops=0;
    double busyUs=0, backlogUs=0, lastStateUs=0, waitSumUs=0;
    uint64_t dequeueCount=0;
    uint32_t previousQueue=0;
    bool transmitting=false;
    double serviceUpdatedUs=0;
    std::string serviceFlow;
    std::map<const QueueDiscItem*,std::string> queuedFlows;
    void ServiceTime() {
        double time=Simulator::Now().GetNanoSeconds()/1000.0;
        if(transmitting) busyUs+=time-serviceUpdatedUs;
        serviceUpdatedUs=time;
    }
    void Observe(const std::string& flow) {if(!flow.empty()) observedPaths[flow].insert(key);}
    void ObserveResidents() {
        ServiceTime();Observe(serviceFlow);
        for(auto& [item,flow]:queuedFlows) Observe(flow);
    }
    Ptr<PcapFileWrapper> capture;
    void Integrate() {
        double t=nowUs(); if (previousQueue) backlogUs += t-lastStateUs;
        previousQueue=disc?disc->GetNPackets():0; lastStateUs=t;
    }
    void Enqueue(Ptr<const QueueDiscItem> item) {
        Integrate();auto flow=identifyItem(item);Observe(flow);
        if(!flow.empty()) queuedFlows[PeekPointer(item)]=flow;
        if (packets.is_open()) record(packets, {{"event","enqueue"},{"queue",key},{"uid",item->GetPacket()->GetUid()},{"bytes",item->GetSize()}});
    }
    void Dequeue(Ptr<const QueueDiscItem> item) {
        Integrate();Observe(identifyItem(item));queuedFlows.erase(PeekPointer(item));
        auto uid=item->GetPacket()->GetUid();
        uint64_t wait=(Simulator::Now()-item->GetTimeStamp()).GetMicroSeconds();
        waitSumUs += wait; ++dequeueCount;
        if (packets.is_open()) record(packets, {{"event","dequeue"},{"queue",key},{"uid",uid},{"bytes",item->GetSize()},{"sojourn_us",wait}});
    }
    void Drop(Ptr<const QueueDiscItem> item) {
        Integrate(); ++drops;Observe(identifyItem(item));queuedFlows.erase(PeekPointer(item));
        if (packets.is_open()) record(packets, {{"event","drop"},{"queue",key},{"uid",item->GetPacket()->GetUid()}});
    }
    void Mark(Ptr<const QueueDiscItem> item, const char*) {
        ++marked; if (packets.is_open()) record(packets, {{"event","mark"},{"queue",key},{"uid",item->GetPacket()->GetUid()}});
    }
    bool Send(Ptr<Packet> p, const Address& dest, uint16_t protocol) override {
        SourceMacTag tag;
        Address source=p->RemovePacketTag(tag)?Address(tag.source):GetAddress();
        return Transmit(p,source,dest,protocol);
    }
    bool SendFrom(Ptr<Packet> p, const Address& source, const Address& dest, uint16_t protocol) override {
        SourceMacTag tag;tag.source=Mac48Address::ConvertFrom(source);p=p->Copy();p->AddPacketTag(tag);
        require(disc!=nullptr,"bridge port missing queue");
        bool accepted=disc->Enqueue(Create<BridgeItem>(p,dest,protocol));disc->Run();return accepted;
    }
    bool Transmit(Ptr<Packet> p, const Address& source, const Address& dest, uint16_t protocol) {
        require(!credit->GetTxQueue(0)->IsStopped(), "device got packet while busy");
        credit->GetTxQueue(0)->Stop();
        double serial = (p->GetSize()+overhead)*8.0/rate;
        bytes += p->GetSize()+overhead;
        ServiceTime();transmitting=true;serviceFlow.clear();
        if(protocol==0x800) {Ipv4Header header;p->PeekHeader(header);serviceFlow=identifyFlow(header);Observe(serviceFlow);}
        if (capture && protocol==0x800) capture->Write(Simulator::Now(),p);
        // Queue-local tags represent host metadata, not bytes transmitted across a link.
        p->RemoveAllPacketTags();
        const bool deliver=up && random->GetValue(0,100)>=loss;
        const double propagation=std::max(0.0,delay + (jitter ? random->GetValue(-jitter,jitter):0));
        // Capture propagation at transmission start; later edits do not move in-flight packets.
        if (deliver) Simulator::ScheduleWithContext(peer->GetNode()->GetId(),NanoSeconds(std::max<int64_t>(1,std::llround((serial+propagation*1000)*1000))),
            [other=peer,p=p->Copy(),protocol,to=Mac48Address::ConvertFrom(dest),from=Mac48Address::ConvertFrom(source)] {
                other->Receive(p,protocol,to,from);
            });
        else { ++deviceDrops; if(packets.is_open()) record(packets,{{"event","link_drop"},{"queue",key},{"uid",p->GetUid()}}); }
        Simulator::ScheduleWithContext(GetNode()->GetId(),NanoSeconds(std::max<int64_t>(1,std::llround(serial*1000))),[this] { ServiceTime();Observe(serviceFlow);serviceFlow.clear();transmitting=false;credit->GetTxQueue(0)->Wake(); });
        return true;
    }
    bool SupportsSendFrom() const override { return true; }
};
NS_OBJECT_ENSURE_REGISTERED(WireDevice);

// ns-3 TCP selects its local address from RouteOutput, even after an explicit Bind.
class SimulationRouting : public Ipv4StaticRouting {
public:
    static TypeId GetTypeId() {static TypeId t=TypeId("MoqlabSimulationRouting").SetParent<Ipv4StaticRouting>().AddConstructor<SimulationRouting>();return t;}
    std::map<uint32_t,Ipv4Address> sources;
    Ptr<Ipv4Route> RouteOutput(Ptr<Packet> packet,const Ipv4Header& header,Ptr<NetDevice> output,Socket::SocketErrno& error) override {
        auto route=Ipv4StaticRouting::RouteOutput(packet,header,output,error);
        if(route && sources.contains(header.GetDestination().Get())) route->SetSource(sources.at(header.GetDestination().Get()));
        return route;
    }
};
class SimulationRoutingHelper : public Ipv4RoutingHelper {
public:
    SimulationRoutingHelper* Copy() const override {return new SimulationRoutingHelper(*this);}
    Ptr<Ipv4RoutingProtocol> Create(Ptr<Node>) const override {return CreateObject<SimulationRouting>();}
};

struct NodeInfo { Ptr<Node> node; Ipv4Address alias; std::map<std::string,Ptr<WireDevice>> ports; };
static std::map<std::string,NodeInfo> nodes;
static std::map<std::string,Ptr<WireDevice>> wires;
static std::map<std::string,Ipv4Address> interfaceIps;
static json queueSpec(const std::string& source,const std::string& target) {
    const auto& e=cfg["experiment"]; std::string key=source+"->"+target;
    if(e["queues"].contains(key)) return e["queues"][key];
    auto q=e["queue"];
    if(cfg["topology"]["routers"].contains(source)) {
        auto router=cfg["topology"]["routers"][source];
        if(!router["aqm"].is_null()) q["kind"]=router["aqm"];
        if(!router["dualpi2_target_ms"].is_null()) q["target_ms"]=router["dualpi2_target_ms"];
    }
    return q;
}
static void installQueue(Ptr<WireDevice> device,json q) {
    TrafficControlHelper helper;
    std::string kind=q["kind"];
    uint32_t limit=q["limit_packets"];
    if(kind=="dualpi2") helper.SetRootQueueDisc("ns3::DualPi2QueueDisc",
        "QueueLimit",UintegerValue(limit*1500),"Target",TimeValue(MilliSeconds(q["target_ms"].get<double>())),
        "L4SMarkThreshold",TimeValue(MilliSeconds(q["dualpi2_step_ms"].get<double>())),
        "Tupdate",TimeValue(MilliSeconds(q["dualpi2_update_ms"].get<double>())),"K",DoubleValue(q["dualpi2_coupling"].get<double>()));
    else if(kind=="red") helper.SetRootQueueDisc("ns3::RedQueueDisc","MaxSize",QueueSizeValue(QueueSize(std::to_string(limit)+"p")),
        "MinTh",DoubleValue(q["red_min_packets"].get<double>()),"MaxTh",DoubleValue(q["red_max_packets"].get<double>()),
        "UseEcn",BooleanValue(q["ecn"]),"LinkBandwidth",DataRateValue(DataRate(uint64_t(device->rate*1e6))),
        "LinkDelay",TimeValue(MilliSeconds(device->delay)));
    else if(kind=="pie") helper.SetRootQueueDisc("ns3::PieQueueDisc","MaxSize",QueueSizeValue(QueueSize(std::to_string(limit)+"p")),
        "UseEcn",BooleanValue(q["ecn"]),"QueueDelayReference",TimeValue(MilliSeconds(q["target_ms"].get<double>())));
    else if(kind=="fq_codel") helper.SetRootQueueDisc("ns3::FqCoDelQueueDisc","MaxSize",QueueSizeValue(QueueSize(std::to_string(limit)+"p")),
        "UseEcn",BooleanValue(q["ecn"]),"Target",StringValue(std::to_string(q["target_ms"].get<double>())+"ms"));
    else helper.SetRootQueueDisc("ns3::FifoQueueDisc","MaxSize",QueueSizeValue(QueueSize(std::to_string(limit)+"p")));
    auto tc=device->GetNode()->GetObject<TrafficControlLayer>();
    if(auto old=tc->GetRootQueueDiscOnDevice(device)) {
        device->Integrate();device->ObserveResidents();helper.Uninstall(device);old->Dispose();device->queuedFlows.clear();device->previousQueue=0;
    }
    device->disc=helper.Install(device).Get(0);
    device->disc->TraceConnectWithoutContext("Enqueue",MakeCallback(&WireDevice::Enqueue,PeekPointer(device)));
    device->disc->TraceConnectWithoutContext("Dequeue",MakeCallback(&WireDevice::Dequeue,PeekPointer(device)));
    device->disc->TraceConnectWithoutContext("Drop",MakeCallback(&WireDevice::Drop,PeekPointer(device)));
    device->disc->TraceConnectWithoutContext("Mark",MakeCallback(&WireDevice::Mark,PeekPointer(device)));
    tc->ScanDevices();
    device->disc->Initialize();
    record(events,{{"event","queue_installed"},{"queue",device->key},{"parameters",q},{"type",device->disc->GetInstanceTypeId().GetName()}});
}
static Ipv4Address address(uint32_t group,uint32_t index) { return Ipv4Address((10u<<24)|(group<<16)|index); }
static void hostRoute(const std::vector<std::string>& path,Ipv4Address dest) {
    for(size_t i=0;i+1<path.size();++i) {
        if(cfg["topology"]["switches"].contains(path[i])) continue;
        size_t j=i+1;
        while(j<path.size() && cfg["topology"]["switches"].contains(path[j])) ++j;
        require(j<path.size(),"route ends on a switch");
        auto& node=nodes.at(path[i]); auto out=node.ports.at(path[i+1]);
        auto gateway=interfaceIps.at(path[j]+"->"+path[j-1]);
        auto ip=node.node->GetObject<Ipv4>();
        auto routing=Ipv4StaticRoutingHelper().GetStaticRouting(ip);
        for(int k=int(routing->GetNRoutes())-1;k>=0;--k) if(routing->GetRoute(k).GetDest()==dest) routing->RemoveRoute(k);
        routing->AddHostRouteTo(dest,gateway,ip->GetInterfaceForDevice(out));
    }
}
struct Endpoint;
struct Flow {
    std::string id,sender,receiver,origin;
    Ipv4Address txIp,rxIp;
    std::vector<std::string> forward,reverse;
    Endpoint *tx=nullptr,*rx=nullptr;
    uint64_t sent=0,received=0,transportAcked=0,transportLost=0,feedbackBytes=0;
    uint64_t measurementStart=0,lastFeedback=0;
    bool measured=false,started=false,closed=false,launched=false;
    sbd::ReceiveIntervals intervals;
    sbd::FeedbackLoss loss;
    std::map<int64_t,sbd::Summary> summaries;
    std::string status="warming_up";
};
static std::vector<std::unique_ptr<Flow>> flows;
static std::map<std::string,uint64_t> available;
static std::map<std::string,bool> originActive;
static std::map<std::string,long double> generated;
static std::map<std::string,std::set<std::string>> latestGroups;
static std::map<std::string,int64_t> lastGroupEnd;
static std::map<std::string,uint64_t> lastGroupDecision;
static std::map<std::string,uint64_t> lastPathChange;
static std::string certDir;
static json transport(const std::string& node) {
    const auto& e=cfg["experiment"]; return e["transport_overrides"].contains(node)?e["transport_overrides"][node]:e["transport"];
}
static void routes(Flow& flow) {
    hostRoute(flow.forward,flow.rxIp); hostRoute(flow.reverse,flow.txIp);
    record(events,{{"event","route"},{"flow",flow.id},{"forward",flow.forward},{"reverse",flow.reverse}});
}
static void reset(Flow& f,const std::string& reason) {
    if(packets.is_open()) record(packets,{{"event","detector_reset"},{"flow",f.id}});
    f.intervals.reset();f.loss.reset();f.summaries.clear();f.measurementStart=nowUs(); f.lastFeedback=nowUs();f.status=reason;
    record(events,{{"event","measurement_reset"},{"flow",f.id},{"reason",reason}});
}
static void be64(std::vector<uint8_t>& data,uint64_t value) { for(int i=7;i>=0;--i) data.push_back(uint8_t(value>>(i*8))); }
static uint64_t read64(const uint8_t* data) { uint64_t value=0;for(int i=0;i<8;++i) value=(value<<8)|data[i];return value; }

struct Endpoint {
    Flow& flow;
    bool sender;
    Ptr<Socket> socket;
    picoquic_quic_t* quic=nullptr;
    picoquic_cnx_t* cnx=nullptr;
    EventId wake;
    uint64_t clock=0;
    bool ready=false,waitingForData=true;
    std::map<uint64_t,uint64_t> sentPackets;
    std::set<uint64_t> receivedPackets;
    std::vector<uint8_t> feedback,feedbackInput;
    uint64_t feedbackAt=0,acknowledgedBytes=0;
    json options;
    Endpoint(Flow& f,bool sending):flow(f),sender(sending),options(transport(sending?f.sender:f.receiver)) {}
    static int Callback(picoquic_cnx_t* c,uint64_t stream,uint8_t* bytes,size_t length,picoquic_call_back_event_t event,void* ctx,void*) {
        return static_cast<Endpoint*>(ctx)->OnCallback(c,stream,bytes,length,event);
    }
    int OnCallback(picoquic_cnx_t* c,uint64_t stream,uint8_t* bytes,size_t length,picoquic_call_back_event_t event) {
        cnx=c;
        if(event==picoquic_callback_ready) {
            ready=true;
            if(sender) {waitingForData=false;require(picoquic_mark_active_stream(c,3,1,nullptr)==0,"activate data stream failed");}
            record(events,{{"event","quic_ready"},{"flow",flow.id},{"side",sender?"sender":"receiver"}});
        }
        if(event==picoquic_callback_close || event==picoquic_callback_application_close || event==picoquic_callback_stateless_reset) {
            ready=false;flow.closed=true;flow.status="closed";cnx=nullptr;
            record(events,{{"event","quic_closed"},{"flow",flow.id},{"error",c->local_error},{"remote_error",c->remote_error}});
        }
        if(event==picoquic_callback_prepare_to_send && sender && stream==3) {
            uint64_t stock=available[flow.origin.starts_with("@")?flow.origin:flow.sender];
            size_t amount=std::min<uint64_t>(length,stock>flow.sent?stock-flow.sent:0);
            waitingForData=stock-flow.sent==amount;
            auto buffer=picoquic_provide_stream_data_buffer(bytes,amount,0,!waitingForData);
            if(amount) {
                require(buffer!=nullptr,"stream buffer failed");
                for(size_t i=0;i<amount;++i) buffer[i]=uint8_t((flow.sent+i)%251);
                if(!flow.started) {flow.started=true;flow.measurementStart=nowUs();flow.lastFeedback=nowUs();}
                flow.sent+=amount;
            }
        }
        if((event==picoquic_callback_stream_data || event==picoquic_callback_stream_fin) && length) {
            if(!sender && stream==3) {
                for(size_t i=0;i<length;++i) require(bytes[i]==uint8_t((flow.received+i)%251),"fan-out payload corruption");
                flow.received+=length; if(!flow.origin.starts_with("@")) available[flow.receiver]=flow.received;
                if(cfg["topology"]["relays"].contains(flow.receiver)) {
                    require(picoquic_set_app_flow_control(c,3,1)==0,"app flow control failed");
                    auto buffer=options["relay_buffer_bytes"].get<uint64_t>();
                    uint64_t floor=flow.received;
                    for(auto& child:flows) if(child->sender==flow.receiver && !child->closed) floor=std::min(floor,child->sent);
                    require(flow.received<=floor+buffer,"relay buffer exceeded");
                }
            } else if(sender && stream==2) {
                feedbackInput.insert(feedbackInput.end(),bytes,bytes+length);
                require(feedbackInput.size()<=options["max_feedback_records"].get<size_t>()*16+16,"feedback input limit");
                std::vector<std::pair<uint64_t,uint64_t>> records;
                size_t used=0;
                while(feedbackInput.size()-used>=16) {records.emplace_back(read64(&feedbackInput[used+8]),read64(&feedbackInput[used]));used+=16;}
                feedbackInput.erase(feedbackInput.begin(),feedbackInput.begin()+used);
                std::sort(records.begin(),records.end());
                std::optional<uint64_t> watermark;
                for(auto [rx,pn]:records) {
                    auto sent=sentPackets.find(pn);
                    if(sent==sentPackets.end() || sent->second<flow.measurementStart) continue;
                    require(rx>=sent->second && rx<=nowUs(),"invalid timestamp feedback");
                    if(!flow.intervals.sample(rx,double(rx-sent->second))) {reset(flow,"waiting_late_feedback");continue;}
                    watermark=rx;flow.lastFeedback=nowUs();
                    if(packets.is_open()) record(packets,{{"event","owd_feedback"},{"flow",flow.id},{"pn",pn},{"sent_us",sent->second},{"receive_us",rx}});
                }
                if(watermark && flow.measured && packets.is_open()) record(packets,{{"event","feedback_watermark"},{"flow",flow.id},{"receive_us",*watermark}});
                if(watermark && flow.measured) for(auto summary:flow.intervals.finishThrough(*watermark)) {
                    flow.loss.apply(summary.intervalEndUs,summary);
                    flow.summaries[summary.intervalEndUs]=summary;
                    while(flow.summaries.size()>110) flow.summaries.erase(flow.summaries.begin());
                    flow.status=!summary.samples?"waiting_receiver_samples":!summary.ready()?"warming_up":summary.measurementValid?"ready":"waiting_previous_interval";
                }
            }
        }
        return 0;
    }
    void Start() {
        auto node=nodes.at(sender?flow.sender:flow.receiver).node;
        socket=Socket::CreateSocket(node,UdpSocketFactory::GetTypeId());
        socket->SetIpRecvTos(true);
        Ipv4Address ip=sender?flow.txIp:flow.rxIp;
        require(socket->Bind(InetSocketAddress(ip,4433))==0,"UDP bind failed");
        socket->SetRecvCallback(MakeCallback(&Endpoint::Receive,this));
        clock=nowUs();
        std::string cert=certDir+"/cert.pem",key=certDir+"/key.pem";
        quic=picoquic_create(8,sender?cert.c_str():nullptr,sender?key.c_str():nullptr,nullptr,"moqlab-simulation",Callback,this,nullptr,nullptr,nullptr,clock,&clock,nullptr,nullptr,0);
        require(quic!=nullptr,"picoquic context creation failed");
        picoquic_set_null_verifier(quic);
        picoquic_set_log_level(quic,1);
        require(picoquic_set_default_congestion_algorithm_by_name(quic,options["congestion_control"].get<std::string>().c_str())==0,"congestion control unavailable");
        picoquic_set_mtu_max(quic,options["mtu_bytes"]);
        uint64_t buffer=options["relay_buffer_bytes"];
        picoquic_set_default_tp_value(quic,picoquic_tp_initial_max_stream_data_uni,buffer);
        picoquic_set_default_tp_value(quic,picoquic_tp_initial_max_data,buffer*2);
        picoquic_set_default_tp_value(quic,picoquic_tp_idle_timeout,600000);
        InstallLogging();
        if(cfg["experiment"]["qlog"].get<bool>()) {
            std::string directory=(output/"qlog").string();std::filesystem::create_directories(directory);
            require(picoquic_set_qlog(quic,directory.c_str())==0,"qlog setup failed");
        }
        if(!sender) {
            sockaddr_in remote{};remote.sin_family=AF_INET;remote.sin_addr.s_addr=htonl(flow.txIp.Get());remote.sin_port=htons(4433);
            cnx=picoquic_create_cnx(quic,picoquic_null_connection_id,picoquic_null_connection_id,reinterpret_cast<sockaddr*>(&remote),clock,0,"simulation","moqlab-simulation",1);
            require(cnx!=nullptr,"QUIC connection creation failed");
            picoquic_set_callback(cnx,Callback,this);
            require(picoquic_start_client_cnx(cnx)==0,"QUIC handshake launch failed");
            RequestWake();
        }
    }
    void Receive(Ptr<Socket> sock) {
        Address from;
        while(auto p=sock->RecvFrom(from)) {
            std::vector<uint8_t> data(p->GetSize());p->CopyData(data.data(),data.size());
            auto remote=InetSocketAddress::ConvertFrom(from);
            sockaddr_in src{},dest{};src.sin_family=dest.sin_family=AF_INET;
            src.sin_addr.s_addr=htonl(remote.GetIpv4().Get());src.sin_port=htons(remote.GetPort());
            dest.sin_addr.s_addr=htonl((sender?flow.txIp:flow.rxIp).Get());dest.sin_port=htons(4433);
            SocketIpTosTag tag;uint8_t ecn=p->PeekPacketTag(tag)?tag.GetTos()&3:0;
            clock=nowUs();
            picoquic_incoming_packet(quic,data.data(),data.size(),reinterpret_cast<sockaddr*>(&src),reinterpret_cast<sockaddr*>(&dest),0,ecn,clock);
        }
        RequestWake();
    }
    void RequestWake() {
        if(wake.IsPending()) wake.Cancel();
        wake=Simulator::Schedule(MicroSeconds(1),&Endpoint::Poll,this);
    }
    void Poll() {
        clock=nowUs();
        for(int i=0;i<128;++i) {
            uint8_t buffer[1536];size_t length=0; sockaddr_storage to{},from{};int index=0;picoquic_connection_id_t cid{};picoquic_cnx_t* last=nullptr;
            int result=picoquic_prepare_next_packet(quic,clock,buffer,sizeof(buffer),&length,&to,&from,&index,&cid,&last);
            require(result==0,"QUIC packet preparation failed"); if(!length) break;
            auto* addr=reinterpret_cast<sockaddr_in*>(&to);
            auto packet=Create<Packet>(buffer,length);
            auto mode=options["ecn"].get<std::string>();socket->SetIpTos(mode=="ect1"?1:mode=="ect0"?2:0);
            require(socket->SendTo(packet,0,InetSocketAddress(Ipv4Address(ntohl(addr->sin_addr.s_addr)),ntohs(addr->sin_port)))==int(length),"simulated UDP send failed");
        }
        auto delay=picoquic_get_next_wake_delay(quic,clock,100000);
        wake=Simulator::Schedule(MicroSeconds(std::max<int64_t>(1,delay)),&Endpoint::Poll,this);
    }
    void Tick() {
        if(!quic) return;
        clock=nowUs();
        if(ready && sender && waitingForData && !flow.closed && available[flow.origin.starts_with("@")?flow.origin:flow.sender]>flow.sent) {
            waitingForData=false;picoquic_mark_active_stream(cnx,3,1,nullptr); RequestWake();
        }
        if(!sender && cnx && ready) {
            if(!feedback.empty() && nowUs()-feedbackAt>=options["feedback_batch_ms"].get<double>()*1000) {
                require(picoquic_add_to_stream(cnx,2,feedback.data(),feedback.size(),0)==0,"feedback stream write failed");
                flow.feedbackBytes+=feedback.size();feedback.clear();feedbackAt=nowUs();RequestWake();
            }
            if(cfg["topology"]["relays"].contains(flow.receiver) && flow.received) {
                uint64_t floor=flow.received,buffer=options["relay_buffer_bytes"];
                for(auto& child:flows) if(child->sender==flow.receiver && !child->closed) floor=std::min(floor,child->sent);
                auto stream=picoquic_find_stream(cnx,3);
                if(stream && floor+buffer>stream->maxdata_local) {
                    require(picoquic_open_flow_control(cnx,3,floor+buffer-flow.received)==0,"flow control credit failed");RequestWake();
                }
            }
        }
        // Bound ledgers to more than the detector's full history, excluding stale feedback.
        auto cutoff=nowUs()>42000000?nowUs()-42000000:0;
        while(!sentPackets.empty() && sentPackets.begin()->second<cutoff) sentPackets.erase(sentPackets.begin());
    }
    static picoquic_unified_logging_t logging;
    void InstallLogging();
    ~Endpoint() {if(quic) picoquic_free(quic);}
};
picoquic_unified_logging_t Endpoint::logging{};
static std::map<picoquic_cnx_t*,Endpoint*> connections;
extern "C" void moqlab_packet_ack(picoquic_cnx_t* cnx,uint64_t pn,uint64_t sent,int spurious) {
    auto found=connections.find(cnx);if(found==connections.end()) return;
    auto* ep=found->second;auto& f=ep->flow;
    if(!ep->sender || !f.measured || !f.started || sent<f.measurementStart) return;
    if(packets.is_open()) record(packets,{{"event",spurious?"transport_spurious":"transport_ack"},{"flow",f.id},{"pn",pn},{"sent_us",sent}});
    bool ok=spurious?f.loss.spuriousLoss(sent,pn):f.loss.acknowledged(sent,pn);
    if(!ok) reset(f,"waiting_outcome_capacity");
    ++f.transportAcked;
}
void Endpoint::InstallLogging() {
    auto& l=logging;
    l.log_quic_app_message=[](auto... ){}; l.log_quic_pdu=[](auto... ){}; l.log_quic_close=[](auto... ){};
    l.log_app_message=[](auto... ){};l.log_pdu=[](auto... ){};
    l.log_dropped_packet=[](auto... ){};l.log_buffered_packet=[](auto... ){};
    l.log_negotiated_alpn=[](auto... ){};l.log_transport_extension=[](auto... ){};l.log_picotls_ticket=[](auto... ){};
    l.log_cc_dump=[](auto... ){};l.log_flush=[](auto... ){};
    l.log_new_connection=[](picoquic_cnx_t* c,void* param,void** ctx) { *ctx=param;connections[c]=static_cast<Endpoint*>(param); };
    l.log_close_connection=[](picoquic_cnx_t* c,void*) {connections.erase(c);};
    l.log_outgoing_packet=[](picoquic_cnx_t*,void* ctx,picoquic_path_t*,uint8_t* bytes,uint64_t pn,size_t,size_t,uint8_t*,size_t,uint64_t time) {
        auto* ep=static_cast<Endpoint*>(ctx);
        if(ep->sender && (bytes[0]&0x80)==0) ep->sentPackets[pn]=time;
    };
    l.log_packet=[](picoquic_cnx_t*,void* ctx,picoquic_path_t*,int receiving,uint64_t time,picoquic_packet_header* ph,const uint8_t* bytes,size_t length) {
        auto* ep=static_cast<Endpoint*>(ctx);
        if(!receiving || ep->sender || ph->ptype!=picoquic_packet_1rtt_protected) return;
        if(!ep->receivedPackets.insert(ph->pn64).second) return;
        // ACK-only packets must not trigger timestamp feedback (feedback ACK loop).
        size_t offset=ph->offset;bool eliciting=false;
        while(offset<length) {size_t used=0;int pure=1;if(picoquic_skip_frame(bytes+offset,length-offset,&used,&pure)!=0 || !used) break;eliciting|=!pure;offset+=used;}
        if(!eliciting) return;
        require(ep->feedback.size()/16<ep->options["max_feedback_records"].get<size_t>(),"feedback output limit");
        be64(ep->feedback,ph->pn64);be64(ep->feedback,time);
        // A sliding PN set bounds duplicate bookkeeping; old packets cannot reach the QUIC callback twice.
        if(ep->receivedPackets.size()>65536) ep->receivedPackets.erase(ep->receivedPackets.begin());
        if(packets.is_open()) record(packets,{{"event","receiver_timestamp"},{"flow",ep->flow.id},{"pn",ph->pn64},{"receive_us",time}});
    };
    l.log_packet_lost=[](picoquic_cnx_t*,void* ctx,picoquic_path_t*,picoquic_packet_type_enum type,uint64_t pn,const char*,picoquic_connection_id_t*,size_t,uint64_t) {
        auto* ep=static_cast<Endpoint*>(ctx);auto& f=ep->flow;
        if(!ep->sender || !f.measured || !f.started || type!=picoquic_packet_1rtt_protected) return;
        auto found=ep->sentPackets.find(pn);if(found==ep->sentPackets.end() || found->second<f.measurementStart) return;
        if(packets.is_open()) record(packets,{{"event","transport_loss"},{"flow",f.id},{"pn",pn},{"sent_us",found->second}});
        ++f.transportLost;if(!f.loss.declaredLost(found->second,pn)) reset(f,"waiting_outcome_capacity");
    };
    require(picoquic_register_log_functions(quic,&logging,this)==0,"packet observer registration failed");
}
static std::vector<std::unique_ptr<Endpoint>> endpoints;

static void makeNetwork() {
    auto t=cfg["topology"],e=cfg["experiment"];
    std::set<std::string> names;
    for(auto group:{"publishers","relays","subscribers","routers","switches"}) for(auto& [name,value]:t[group].items()) names.insert(name);
    if(!t["traffic"].is_null()) for(auto group:{"senders","receivers"}) for(auto endpoint:t["traffic"][group]) names.insert(endpoint["id"]);
    for(auto& name:names) {
        auto n=CreateObject<Node>();nodes[name].node=n;
        if(!t["switches"].contains(name)) {
            InternetStackHelper stack;stack.SetIpv6StackInstall(false);stack.SetRoutingHelper(SimulationRoutingHelper());stack.Install(n);
            auto ip=n->GetObject<Ipv4>();ip->SetAttribute("IpForward",BooleanValue(t["routers"].contains(name)));
        } else n->AggregateObject(CreateObject<TrafficControlLayer>());
    }
    uint32_t idx=1;int64_t stream=1;
    for(auto link:t["links"]) {
        std::string a=link["from"],b=link["to"];
        auto channel=CreateObject<SimpleChannel>();
        auto first=CreateObject<WireDevice>(),second=CreateObject<WireDevice>();first->peer=second;second->peer=first;
        for(auto [source,target,device,direction]:{std::tuple{a,b,first,std::string("forward")},std::tuple{b,a,second,std::string("reverse")}}) {
            device->SetMtu(e["link_mtu_bytes"].get<uint16_t>());
            device->key=source+"->"+target;device->SetAddress(Mac48Address::Allocate());
            device->credit=CreateObject<NetDeviceQueueInterface>();device->AggregateObject(device->credit);
            nodes[source].node->AddDevice(device);device->SetChannel(channel);device->random->SetStream(stream++);
            auto d=link[direction];
            device->rate=d["bandwidth_mbps"].is_null()?e["default_bandwidth_mbps"].get<double>():d["bandwidth_mbps"].get<double>();
            device->delay=d["delay_ms"].is_null()?e["default_delay_ms"].get<double>():d["delay_ms"].get<double>();
            device->jitter=d["jitter_ms"].is_null()?0:d["jitter_ms"].get<double>();
            device->loss=d["loss_pct"].is_null()?0:d["loss_pct"].get<double>();device->overhead=e["wire_overhead_bytes"];
            nodes[source].ports[target]=device;wires[device->key]=device;
            if(!t["switches"].contains(source)) {
                auto ip=nodes[source].node->GetObject<Ipv4>();int i=ip->AddInterface(device);
                auto addr=address(20,idx++);ip->AddAddress(i,Ipv4InterfaceAddress(addr,Ipv4Mask("255.255.255.255")));
                ip->SetUp(i);ip->SetForwarding(i,t["routers"].contains(source));interfaceIps[device->key]=addr;
            }
            if(e["pcap"].get<bool>()) {
                device->capture=CreateObject<PcapFileWrapper>();
                device->capture->Open((output/(source+"--"+target+".pcap")).string(),std::ios::out);
                device->capture->Init(101); // DLT_RAW: IP packets, without synthetic wire overhead.
            }
        }
    }
    for(auto& [name,value]:t["switches"].items()) {
        NetDeviceContainer ports;for(auto& [target,port]:nodes[name].ports) ports.Add(port);
        BridgeHelper().Install(nodes[name].node,ports);
    }
    for(auto& [key,wire]:wires) {
        auto pos=key.find("->");installQueue(wire,queueSpec(key.substr(0,pos),key.substr(pos+2)));
    }
}
static Ipv4Address alias(const std::string& node,uint32_t index) {
    auto ip=nodes.at(node).node->GetObject<Ipv4>();auto addr=address(250,index);
    ip->AddAddress(0,Ipv4InterfaceAddress(addr,Ipv4Mask("255.255.255.255")));return addr;
}
static void makeFlows() {
    uint32_t index=1;
    for(const auto& app:cfg["applications"]) {
        auto f=std::make_unique<Flow>();f->id=app["id"];f->sender=app["sender"];f->receiver=app["receiver"];f->origin=app["origin"];
        auto relay=cfg["topology"]["relays"].find(f->sender);
        f->measured=!f->origin.starts_with("@") && relay!=cfg["topology"]["relays"].end() && (*relay)["sbd"]["enabled"].get<bool>();
        f->txIp=alias(f->sender,index++);f->rxIp=alias(f->receiver,index++);
        if(f->measured) forwardFlowIds[(uint64_t(f->txIp.Get())<<32)|f->rxIp.Get()]=f->id;
        for(auto route:cfg["experiment"]["routes"]) if(route["sender"]==f->sender && route["receiver"]==f->receiver) {
            f->forward=route["forward"].get<std::vector<std::string>>();f->reverse=route["reverse"].get<std::vector<std::string>>();
        }
        require(!f->forward.empty(),"missing resolved route");routes(*f);
        endpoints.push_back(std::make_unique<Endpoint>(*f,true));f->tx=endpoints.back().get();
        endpoints.push_back(std::make_unique<Endpoint>(*f,false));f->rx=endpoints.back().get();
        flows.push_back(std::move(f));
    }
    for(auto& f:flows) {
        json timing={{"start_s",0},{"stop_s",nullptr}};
        auto configured=cfg["experiment"]["connections"].find(f->id);
        if(configured!=cfg["experiment"]["connections"].end()) timing=*configured;
        Simulator::Schedule(Seconds(timing["start_s"].get<double>()),[flow=f.get()] {
            flow->tx->Start();flow->rx->Start();flow->launched=true;record(events,{{"event","connection_start"},{"flow",flow->id}});
        });
        if(!timing["stop_s"].is_null()) Simulator::Schedule(Seconds(timing["stop_s"].get<double>()),[flow=f.get()] {
            flow->closed=true;flow->status="closed";
            if(flow->tx->cnx) picoquic_close(flow->tx->cnx,0);
            if(flow->rx->cnx) picoquic_close(flow->rx->cnx,0);
            record(events,{{"event","connection_stop"},{"flow",flow->id}});
        });
    }
}
struct Traffic {
    std::string id,kind,source,target;
    Ptr<Socket> tx,rx,listener;
    std::vector<Ptr<Socket>> accepted;
    Ipv4Address txIp,rxIp;
    uint64_t sent=0,received=0;
    double start=0,end=0,rate=0,segmentMs=0;
    uint32_t packetSize=1200;
    std::vector<double> representations;
    std::vector<std::string> forward,reverse;
    void Read(Ptr<Socket> socket) {while(auto p=socket->Recv()) {if(!p->GetSize()) break;received+=p->GetSize();}}
    bool Accept(Ptr<Socket>,const Address&) {return true;}
    void Accepted(Ptr<Socket> socket,const Address&) {accepted.push_back(socket);socket->SetRecvCallback(MakeCallback(&Traffic::Read,this));}
    void Connected(Ptr<Socket>) {Pump();}
    void CanSend(Ptr<Socket>,uint32_t) {Pump();}
    uint64_t Target() const {
        double t=Simulator::Now().GetSeconds()-start;
        if(t<0) return 0;
        if(kind=="bulk") return std::numeric_limits<uint64_t>::max();
        if(kind=="cbr") return uint64_t(t*rate*1e6/8)+packetSize;
        auto index=uint64_t(t*1000/segmentMs);long double total=0;
        uint64_t cycles=index/representations.size();
        total=cycles*std::accumulate(representations.begin(),representations.end(),0.0)*1e6*segmentMs/8000;
        for(size_t i=0;i<=index%representations.size();++i) total+=representations[i]*1e6*segmentMs/8000;
        return uint64_t(total);
    }
    void Pump() {
        double t=Simulator::Now().GetSeconds();if(t<start || t>=end) return;
        for(int i=0;i<128 && sent<Target();++i) {
            uint64_t amount=std::min<uint64_t>(kind=="cbr"?packetSize:65536,Target()-sent);
            if(kind!="cbr") amount=std::min<uint64_t>(amount,tx->GetTxAvailable());
            if(!amount) break;
            auto result=tx->Send(Create<Packet>(amount));if(result<=0) break;sent+=result;
            if(kind=="cbr") break;
        }
    }
    void UdpTick() {
        Pump();double spacing=packetSize*8.0/(rate*1e6);
        if(Simulator::Now().GetSeconds()+spacing<end) Simulator::Schedule(Seconds(spacing),&Traffic::UdpTick,this);
    }
};
static std::vector<std::unique_ptr<Traffic>> traffic;
static void makeTraffic() {
    auto t=cfg["topology"]["traffic"];if(t.is_null()) return;
    uint32_t index=1+2*flows.size();
    for(auto spec:t["flows"]) {
        auto path=t["routes"][spec["route"].get<std::string>()]["path"].get<std::vector<std::string>>();
        int count=spec["kind"]=="bulk"?spec["connections"].get<int>():spec["kind"]=="segmented"?spec["clients"].get<int>():1;
        for(int i=0;i<count;++i) {
            auto load=std::make_unique<Traffic>();load->id=spec["id"].get<std::string>()+"/"+std::to_string(i);
            load->kind=spec["kind"];load->source=path.front();load->target=path.back();load->start=spec["start_s"];load->end=load->start+spec["duration_s"].get<double>();
            load->txIp=alias(load->source,index++);load->rxIp=alias(load->target,index++);
            DynamicCast<SimulationRouting>(nodes[load->source].node->GetObject<Ipv4>()->GetRoutingProtocol())->sources[load->rxIp.Get()]=load->txIp;
            DynamicCast<SimulationRouting>(nodes[load->target].node->GetObject<Ipv4>()->GetRoutingProtocol())->sources[load->txIp.Get()]=load->rxIp;
            load->forward=path;load->reverse=path;std::reverse(load->reverse.begin(),load->reverse.end());
            hostRoute(path,load->rxIp);auto reverse=path;std::reverse(reverse.begin(),reverse.end());hostRoute(reverse,load->txIp);
            bool udp=load->kind=="cbr";auto factory=udp?UdpSocketFactory::GetTypeId():TcpSocketFactory::GetTypeId();
            load->tx=Socket::CreateSocket(nodes[load->source].node,factory);
            load->rx=Socket::CreateSocket(nodes[load->target].node,factory);
            require(load->tx->Bind(InetSocketAddress(load->txIp,9000))==0,"traffic sender bind");
            require(load->rx->Bind(InetSocketAddress(load->rxIp,9000))==0,"traffic receiver bind");
            load->rx->SetRecvCallback(MakeCallback(&Traffic::Read,load.get()));
            if(!udp) {
                load->rx->Listen();load->rx->SetAcceptCallback(MakeCallback(&Traffic::Accept,load.get()),MakeCallback(&Traffic::Accepted,load.get()));
                load->tx->SetSendCallback(MakeCallback(&Traffic::CanSend,load.get()));
                load->tx->SetConnectCallback(MakeCallback(&Traffic::Connected,load.get()),MakeNullCallback<void,Ptr<Socket>>());
            } else {load->rate=spec["rate_mbps"];load->packetSize=spec["packet_size_bytes"];}
            if(load->kind=="segmented") {load->segmentMs=spec["segment_duration_ms"];load->representations=spec["representation_sequence_mbps"].get<std::vector<double>>();}
            auto* pointer=load.get();
            Simulator::Schedule(Seconds(load->start),[pointer,udp] {
                require(pointer->tx->Connect(InetSocketAddress(pointer->rxIp,9000))==0,"traffic connect");
                record(events,{{"event","traffic_start"},{"id",pointer->id}});if(udp) pointer->UdpTick();
            });
            Simulator::Schedule(Seconds(load->end),[pointer] {pointer->tx->Close();record(events,{{"event","traffic_stop"},{"id",pointer->id}});});
            traffic.push_back(std::move(load));
        }
    }
}
static void tick() {
    constexpr double dt=.001;
    double time=Simulator::Now().GetSeconds();const auto& e=cfg["experiment"];
    for(auto& [name,pub]:cfg["topology"]["publishers"].items()) {
        auto work=e["workloads"].contains(name)?e["workloads"][name]:e["workload"];
        double start=work["start_s"],stop=work["stop_s"].is_null()?e["duration_s"].get<double>():work["stop_s"].get<double>();
        if(time<start || time>=stop || !originActive[name]) {
            if(work["kind"]=="bulk") {
                uint64_t issued=0;for(auto& f:flows) if(f->sender==name) issued=std::max(issued,f->sent);
                available[name]=issued;
            }
            continue;
        }
        std::string kind=work["kind"];
        if(kind=="bulk") available[name]=std::numeric_limits<uint64_t>::max()/2;
        else if(kind=="paced") {
            generated[name]+=work["rate_mbps"].get<double>()*1e6/8*dt;
            available[name]=uint64_t(generated[name]);
        } else {
            auto reps=work["representation_sequence_mbps"].get<std::vector<double>>();double segment=work["segment_duration_ms"].get<double>()/1000;
            uint64_t index=uint64_t((time-start)/segment),cycles=index/reps.size();
            long double total=cycles*std::accumulate(reps.begin(),reps.end(),0.0)*segment*1e6/8;
            for(size_t i=0;i<=index%reps.size();++i) total+=reps[i]*segment*1e6/8;
            available[name]=uint64_t(total);
        }
    }
    for(const auto& app:cfg["applications"]) if(app["origin"].get<std::string>().starts_with("@")) {
        bool active=time>=app["start_s"].get<double>() && time<app["stop_s"].get<double>();
        // Stop adding data at the deadline; bytes already queued in QUIC can drain.
        if(active) available[app["origin"]]=std::numeric_limits<uint64_t>::max()/2;
        else for(auto& f:flows) if(f->id==app["id"].get<std::string>()) available[f->origin]=f->sent;
    }
    for(auto& ep:endpoints) ep->Tick();
    for(auto& load:traffic) if(load->kind!="cbr") load->Pump();
    Simulator::Schedule(MilliSeconds(1),tick);
}
static void sample() {
    auto eval=cfg["experiment"]["evaluation"];
    double window=eval["window_ms"].get<double>()*1000;
    std::set<std::string> active;
    for(auto& [key,wire]:wires) {
        wire->Integrate();wire->ObserveResidents();double utilization=std::min(1.0,wire->busyUs/window),backlog=std::min(1.0,wire->backlogUs/window);
        double sojourn=wire->dequeueCount?wire->waitSumUs/wire->dequeueCount:0;
        bool congested=utilization>=eval["utilization_threshold"].get<double>() && backlog>=eval["backlog_fraction_threshold"].get<double>() && sojourn>=eval["sojourn_threshold_ms"].get<double>()*1000;
        if(congested) active.insert(key);
        record(queues,{{"queue",key},{"window_us",window},{"wire_bytes",wire->bytes},{"utilization",utilization},{"backlog_fraction",backlog},
            {"mean_sojourn_us",sojourn},{"queue_bytes",wire->disc->GetNBytes()},{"drops",wire->drops},{"link_drops",wire->deviceDrops},{"marks",wire->marked},{"active",congested},{"capacity_mbps",wire->rate}});
        wire->bytes=wire->drops=wire->marked=wire->deviceDrops=wire->dequeueCount=0;wire->busyUs=wire->backlogUs=wire->waitSumUs=0;
    }
    for(auto& [server,value]:cfg["topology"]["relays"].items()) {
        if(!value["sbd"]["enabled"].get<bool>()) continue;
        std::vector<Flow*> ready;
        json states=json::array();
        for(auto& f:flows) if(f->sender==server && f->measured) {
            bool fresh=f->started && !f->closed && nowUs()-f->lastFeedback<3*sbd::kIntervalUs;
            bool usable=fresh && f->status=="ready" && !f->summaries.empty();
            if(usable) ready.push_back(f.get());
            json state={{"id",f->id},{"status",!fresh?(f->closed?"closed":"waiting_feedback"):f->status},{"known",false}};
            if(!f->summaries.empty()) {
                auto summary=f->summaries.rbegin()->second;
                state["interval_end_us"]=summary.intervalEndUs;state["intervals"]=summary.intervals;
                state["skew"]=summary.skew;state["pdv2_us"]=summary.variation;state["frequency"]=summary.frequency;
                state["loss"]=summary.loss;state["bottleneck"]=summary.bottleneck;
            }
            states.push_back(state);
        }
        int64_t common=0;
        if(!ready.empty()) for(auto it=ready.front()->summaries.rbegin();it!=ready.front()->summaries.rend();++it) {
            auto end=it->first;
            bool present=std::all_of(ready.begin(),ready.end(),[end](Flow* f) {return f->summaries.contains(end) && f->summaries.at(end).groupingReady();});
            if(present) {common=end;break;}
        }
        json groups=json::array();
        if(common && common>=lastGroupEnd[server]) {
            std::vector<sbd::FlowSummary> summaries;
            for(auto* f:ready) summaries.push_back({f->id,f->summaries.at(common)});
            groups=sbd::group(summaries);
            if(common>lastGroupEnd[server]) {lastGroupEnd[server]=common;lastGroupDecision[server]=nowUs();}
            for(auto& state:states) for(auto* f:ready) if(state["id"]==f->id) state["known"]=true;
        }
        record(snapshots,{{"algorithm","PracticalPassiveAndRFC"},{"server",server},{"interval_end_us",common},{"decision_us",lastGroupDecision[server]},
            {"groups",groups},{"flows",states}});
        json paths=json::object(),observed=json::object();
        for(auto& f:flows) if(f->sender==server && f->measured && f->launched && !f->closed) {
            std::vector<std::string> path;
            for(size_t i=0;i+1<f->forward.size();++i) path.push_back(f->forward[i]+"->"+f->forward[i+1]);
            paths[f->id]=path;observed[f->id]=observedPaths[f->id];
        }
        record(truths,{{"server",server},{"window_us",window},{"flow_paths",paths},{"observed_paths",observed},{"active_queues",active}});
    }
    observedPaths.clear();
    for(auto& f:flows) {
        json value={{"flow",f->id},{"sent",f->sent},{"received",f->received},{"transport_acked_packets",f->transportAcked},{"transport_lost_packets",f->transportLost},{"feedback_bytes",f->feedbackBytes}};
        if(!f->closed && f->tx->cnx && f->tx->cnx->nb_paths>0) {auto path=f->tx->cnx->path[0];value["cwnd_bytes"]=path->cwin;value["smoothed_rtt_us"]=path->smoothed_rtt;value["pacing_rate_Bps"]=path->pacing.rate;}
        record(applications,value);
    }
    Simulator::Schedule(MilliSeconds(eval["window_ms"].get<double>()),sample);
}
static void scheduleEvents() {
    for(auto event:cfg["experiment"]["events"]) Simulator::Schedule(Seconds(event["time_s"].get<double>()),[event] {
        record(events,{{"event","experiment_event"},{"parameters",event}});
        std::string kind=event["kind"];
        if(kind=="flow") {originActive[event["origin"]]=event["active"];return;}
        if(kind=="route") {
            for(auto& f:flows) if(f->sender==event["sender"].get<std::string>() && f->receiver==event["receiver"].get<std::string>()) {
                f->forward=event["forward"].get<std::vector<std::string>>();f->reverse=event["reverse"].get<std::vector<std::string>>();routes(*f);
            }
            for(auto& load:traffic) if(load->source==event["sender"].get<std::string>() && load->target==event["receiver"].get<std::string>()) {
                load->forward=event["forward"].get<std::vector<std::string>>();load->reverse=event["reverse"].get<std::vector<std::string>>();
                hostRoute(load->forward,load->rxIp);hostRoute(load->reverse,load->txIp);
            }
            return;
        }
        auto wire=wires.at(event["source"].get<std::string>()+"->"+event["target"].get<std::string>());
        if(kind=="link") {
            if(!event["bandwidth_mbps"].is_null()) wire->rate=event["bandwidth_mbps"];
            if(!event["delay_ms"].is_null()) wire->delay=event["delay_ms"];
            if(!event["jitter_ms"].is_null()) wire->jitter=event["jitter_ms"];
            if(!event["loss_pct"].is_null()) wire->loss=event["loss_pct"];
            if(!event["up"].is_null()) wire->up=event["up"];
        } else if(kind=="queue") {
            require(!wire->disc->GetNPackets() || event["backlog_policy"]=="flush","AQM replacement rejected: nonempty queue");
            if(wire->disc->GetNPackets()) record(events,{{"event","queue_flush"},{"queue",wire->key},{"packets",wire->disc->GetNPackets()},{"bytes",wire->disc->GetNBytes()}});
            installQueue(wire,event["queue"]);
        }
    });
}
#include "test_wire.inc"
int main(int argc,char** argv) {
    try {
        if(argc==2 && std::string(argv[1])=="--check-wire") return checkWire();
        require(argc==4,"usage: moqlab-sim resolved.json output-directory picoquic-certs");
        std::ifstream input(argv[1]);input>>cfg;output=argv[2];certDir=argv[3];
        require(std::filesystem::is_directory(output),"output directory must exist");
        events.open(output/"events.jsonl");queues.open(output/"queues.jsonl");snapshots.open(output/"sbd.jsonl");truths.open(output/"truth.jsonl");applications.open(output/"applications.jsonl");
        if(cfg["experiment"]["trace"]=="packets") packets.open(output/"packets.jsonl");
        picoquic_register_all_congestion_control_algorithms();
        simulationRandom=cfg["experiment"]["seed"].get<uint64_t>()*0x9e3779b97f4a7c15ULL+cfg.value("run_number",1);
        picoquic_tls_api_init();picoquic_register_crypto_random_provider_fn(simulationRandomBytes);
        picoquic_public_random_seed_64(simulationRandom,1);
        RngSeedManager::SetSeed(cfg["experiment"]["seed"].get<uint32_t>());
        RngSeedManager::SetRun(cfg.value("run_number",1));
        Config::SetDefault("ns3::TcpL4Protocol::SocketType",TypeIdValue(TcpCubic::GetTypeId()));
        Config::SetDefault("ns3::TcpSocketBase::UseEcn",EnumValue(TcpSocketState::On));
        for(auto& [name,pub]:cfg["topology"]["publishers"].items()) originActive[name]=true;
        makeNetwork();makeFlows();makeTraffic();scheduleEvents();
        Simulator::Schedule(MilliSeconds(1),tick);
        Simulator::Schedule(MilliSeconds(cfg["experiment"]["evaluation"]["window_ms"].get<double>()),sample);
        Simulator::Stop(Seconds(cfg["experiment"]["duration_s"].get<double>()));
        Simulator::Run();
        json summary=json::array();
        for(auto& f:flows) {require(f->received<=f->sent,"QUIC byte accounting");summary.push_back({{"id",f->id},{"sent",f->sent},{"received",f->received},{"feedback_bytes",f->feedbackBytes},{"closed",f->closed}});}
        for(auto& t:traffic) {require(t->received<=t->sent,"traffic byte accounting");summary.push_back({{"id",t->id},{"sent",t->sent},{"received",t->received}});}
        record(events,{{"event","simulation_complete"},{"flows",summary}});
        for(auto* stream:{&events,&packets,&queues,&snapshots,&truths,&applications}) if(stream->is_open()) {stream->flush();require(bool(*stream),"output flush failed");}
        for(auto& ep:endpoints) if(ep->wake.IsPending()) ep->wake.Cancel();
        endpoints.clear();
        for(auto& [key,wire]:wires) if(wire->capture) {wire->capture->Close();wire->capture=nullptr;}
        Simulator::Destroy();
        for(auto& [key,wire]:wires) wire->peer=nullptr;
        std::ofstream done(output/"COMPLETE");done<<"simulation complete\n";require(bool(done),"completion write failed");
        return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<'\n';return 1;}
}
