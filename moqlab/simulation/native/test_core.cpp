// SPDX-License-Identifier: Apache-2.0
#include "sbd/ReceiveIntervals.h"
#include <cmath>
#include <stdexcept>
#include <iostream>
namespace sbd=openmoq::moqx::sbd;
static void check(bool condition,const char* label) {if(!condition) throw std::runtime_error(label);}
int main() {
    sbd::Detector detector,shifted;
    for(int i=0;i<130;++i) {
        double minimum=1e100,maximum=-1e100,total=0;
        for(int j=0;j<25;++j) {
            double value=10000+1000*std::sin(i/7.0)+j*5;
            detector.sample(value);shifted.sample(value+123456);
            minimum=std::min(minimum,value);maximum=std::max(maximum,value);total+=value;
        }
        detector.outcomes(25,1);shifted.outcomes(25,1);
        auto a=detector.finishInterval(),b=shifted.finishInterval();
        check(std::abs(a.intervalMeanDelayUs-total/25)<1e-8,"independent interval mean");
        check(a.intervalMinDelayUs==minimum && a.intervalMaxDelayUs==maximum,"independent interval extrema");
        check(std::abs(a.skew-b.skew)<1e-8 && std::abs(a.variation-b.variation)<1e-8,"offset invariance");
        check(a.ready()==(i>=49) && a.groupingReady()==(i>=99),"warm-up boundary");
        check(std::abs(a.loss-1.0/26)<1e-10,"independent loss fraction");
    }
    check(detector.finishInterval().intervals==0,"empty interval restarts warm-up");
    sbd::FeedbackLoss loss; sbd::Summary summary;
    check(loss.declaredLost(400000,7),"lost packet");loss.apply(700000,summary);check(summary.lost==1,"loss ledger");
    check(loss.spuriousLoss(400000,7),"spurious packet");loss.apply(700000,summary);check(summary.lost==0 && summary.acked==1,"spurious correction");
    check(loss.acknowledged(400000,7),"duplicate ack");loss.apply(700000,summary);check(summary.acked==1,"outcome deduplication");
    sbd::ReceiveIntervals received;
    check(received.sample(100000,100),"partial interval sample");
    check(received.sample(400000,200),"complete interval sample");
    check(received.finishThrough(699999).empty(),"no premature watermark publication");
    auto values=received.finishThrough(700000);check(values.size()==1 && values[0].samples==1 && values[0].intervalStartUs==350000,"watermark boundary");
    check(!received.sample(500000,200),"late feedback rejected");
    check(!received.sample(710000,NAN),"invalid measurement rejected");
    sbd::Summary same;same.intervals=100;same.bottleneck=true;same.groupingVariation=10;same.skew=-.2;same.frequency=.1;
    check(sbd::group({{"a",same},{"b",same}}).size()==1,"grouping reference");
    std::cout<<"PASS SBD independent summaries, warm-up, watermark, loss correction and grouping\n";
}
