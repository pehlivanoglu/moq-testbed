// SPDX-License-Identifier: GPL-2.0-only
static std::vector<uint64_t> wireReceived;
static Ptr<WireDevice> testWire;
static unsigned testSent=0;
static bool receiveTest(Ptr<NetDevice> device,Ptr<const Packet> packet,uint16_t,const Address& source) {
    require(Simulator::GetContext()==device->GetNode()->GetId(),"receive node context");
    require(packet->GetSize()==1000,"wire payload length");
    require(Mac48Address::ConvertFrom(source)==Mac48Address::ConvertFrom(testWire->GetAddress()),"L2 source preservation");
    SocketIpTosTag tag;require(!packet->PeekPacketTag(tag),"host-only tags leaked over link");
    wireReceived.push_back(nowUs());return true;
}
static void sendTest() {
    if(testSent==2) return;
    ++testSent;auto p=Create<Packet>(1000);SocketIpTosTag tag;tag.SetTos(2);p->AddPacketTag(tag);testWire->Send(p,testWire->peer->GetAddress(),0x88b5);
}
static int checkWire() {
    auto a=CreateObject<Node>(),b=CreateObject<Node>();
    auto first=CreateObject<WireDevice>(),second=CreateObject<WireDevice>();
    auto channel=CreateObject<SimpleChannel>();first->peer=second;second->peer=first;
    first->SetAddress(Mac48Address::Allocate());second->SetAddress(Mac48Address::Allocate());
    a->AddDevice(first);b->AddDevice(second);first->SetChannel(channel);second->SetChannel(channel);
    first->credit=CreateObject<NetDeviceQueueInterface>();first->AggregateObject(first->credit);
    require(first->GetMtu()==1500,"explicit Ethernet MTU calibration");
    first->rate=1;first->delay=5;first->overhead=0;
    second->SetReceiveCallback(MakeCallback(&receiveTest));testWire=first;
    first->credit->GetTxQueue(0)->SetWakeCallback(MakeCallback(&sendTest));
    double firstWindowBusy=0;
    Simulator::ScheduleNow(sendTest);
    Simulator::Schedule(MilliSeconds(4),[first,&firstWindowBusy] {
        first->ServiceTime();firstWindowBusy=first->busyUs;
        require(firstWindowBusy==4000,"service time crossing an observation window");first->busyUs=0;
    });
    Simulator::Schedule(MilliSeconds(1),[first]{first->rate=2;first->delay=10;});
    Simulator::Stop(MilliSeconds(30));Simulator::Run();
    require(wireReceived==std::vector<uint64_t>{13000,22000},"serialization, propagation, or in-flight update timing incorrect");
    require(first->bytes==2000 && firstWindowBusy+first->busyUs==12000,"wire byte/time accounting");
    Simulator::Destroy();first->peer=nullptr;second->peer=nullptr;testWire=nullptr;
    std::cout<<"PASS wire serialization, propagation, live edits, byte accounting and source MAC\n";return 0;
}
