// SPDX-License-Identifier: Apache-2.0
#include "sbd/ReceiveIntervals.h"
#include <nlohmann/json.hpp>
#include <fstream>
#include <map>
#include <iostream>
#include <cmath>
#include <stdexcept>
using json=nlohmann::json;
namespace sbd=openmoq::moqx::sbd;
struct Flow {sbd::ReceiveIntervals receiver;sbd::FeedbackLoss loss;std::map<int64_t,sbd::Summary> history;};
int main(int argc,char** argv) {
    try {
        if(argc!=3) throw std::runtime_error("usage: sbd-replay packets.jsonl sbd.jsonl");
        std::map<uint64_t,json> records;
        for(int i=1;i<=2;++i) {std::ifstream file(argv[i]);std::string line;while(std::getline(file,line)) {auto item=json::parse(line);records[item.at("sequence")]=item;}}
        std::map<std::string,Flow> flows;size_t checks=0;
        for(auto& [sequence,item]:records) {
            if(item.contains("algorithm")) {
                for(auto value:item["flows"]) if(value.contains("interval_end_us")) {
                    auto& flow=flows[value["id"]];auto end=value["interval_end_us"].get<int64_t>();
                    if(!flow.history.contains(end)) throw std::runtime_error("replay missing interval");
                    auto summary=flow.history[end];
                    if(summary.intervals!=value["intervals"].get<uint64_t>()) throw std::runtime_error("replay interval count mismatch");
                    for(auto [actual,expected]: {std::pair{summary.skew,value["skew"].get<double>()},std::pair{summary.variation,value["pdv2_us"].get<double>()},std::pair{summary.frequency,value["frequency"].get<double>()},std::pair{summary.loss,value["loss"].get<double>()}}) {
                        if(std::abs(actual-expected)>1e-7) throw std::runtime_error("replay metric mismatch");
                    }
                    ++checks;
                }
                continue;
            }
            if(!item.contains("flow")) continue;
            auto& flow=flows[item["flow"]];std::string event=item["event"];
            if(event=="detector_reset") {flow=Flow{};}
            if(event=="owd_feedback") {if(!flow.receiver.sample(item["receive_us"],item["receive_us"].get<double>()-item["sent_us"].get<double>())) throw std::runtime_error("replay sample rejected");}
            if(event=="transport_ack" || event=="transport_spurious") {if(!flow.loss.acknowledged(item["sent_us"],item["pn"])) throw std::runtime_error("replay ack rejected");}
            if(event=="transport_loss") {if(!flow.loss.declaredLost(item["sent_us"],item["pn"])) throw std::runtime_error("replay loss rejected");}
            if(event=="feedback_watermark") for(auto summary:flow.receiver.finishThrough(item["receive_us"])) {flow.loss.apply(summary.intervalEndUs,summary);flow.history[summary.intervalEndUs]=summary;}
        }
        if(!checks) throw std::runtime_error("no replay comparisons");
        std::cout<<"PASS "<<checks<<" interval summaries replayed from timestamp feedback and transport outcomes\n";
        return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<'\n';return 1;}
}
