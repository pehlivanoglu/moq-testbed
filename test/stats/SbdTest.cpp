/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/Detector.h"
#include "sbd/ReceiveIntervals.h"
#include "stats/ClientNetworkMetrics.h"
#include <gtest/gtest.h>
#include <folly/json.h>
#include <cmath>
#include <limits>

using namespace openmoq::moqx;
using namespace openmoq::moqx::sbd;
TEST(Sbd, OffsetInvarianceAndWarmup) {
  Detector a, b;
  for (int interval = 0; interval < 100; ++interval) {
    for (int p = 0; p < 20; ++p) {
      const double delay = 5000 + 1000 * std::sin(interval / 3.0) + p * 5;
      a.sample(delay); b.sample(delay - 9876543);
    }
    a.outcomes(20, 1); b.outcomes(20, 1);
    auto x = a.finishInterval(), y = b.finishInterval();
    EXPECT_EQ(x.ready(), interval >= 49);
    EXPECT_NEAR(x.skew, y.skew, 1e-10);
    EXPECT_NEAR(x.variation, y.variation, 1e-7);
    EXPECT_EQ(x.frequency, y.frequency);
    EXPECT_DOUBLE_EQ(x.loss, 1.0 / 21);
  }
}
TEST(Sbd, IntervalSkewUsesPreviousMeanAndPdv2) {
  Detector d;
  d.sample(10); d.finishInterval();
  d.sample(20); d.sample(20);
  auto s = d.finishInterval();
  EXPECT_DOUBLE_EQ(s.skew, -0.5);
  EXPECT_DOUBLE_EQ(s.variation, 0);
  d.sample(0);
  s = d.finishInterval();
  EXPECT_DOUBLE_EQ(s.skew, 0);
  EXPECT_DOUBLE_EQ(s.variation, 0);
  d.sample(0); d.sample(10);
  s = d.finishInterval();
  EXPECT_DOUBLE_EQ(s.variation, 5.0 / 4);
}
TEST(Sbd, MissingIntervalResetsReadiness) {
  Detector d;
  for (int i = 0; i < 60; ++i) { d.sample(i); d.finishInterval(); }
  auto s = d.finishInterval();
  EXPECT_EQ(s.intervals, 0);
  EXPECT_FALSE(s.ready());
  d.sample(3);
  EXPECT_EQ(d.finishInterval().intervals, 1);
}
TEST(Sbd, PacketLossHasFiftyIntervalWindow) {
  Detector d;
  for (int i = 0; i < 51; ++i) {
    d.sample(100 + i); d.outcomes(9, i == 0 ? 1 : 0);
    auto s = d.finishInterval();
    EXPECT_EQ(s.lost, i < 50 ? 1 : 0);
    EXPECT_EQ(s.acked, 9 * std::min(i + 1, 50));
    EXPECT_DOUBLE_EQ(s.loss, i < 50 ? 1.0 / (9 * (i + 1) + 1) : 0.0);
  }
}
TEST(Sbd, GroupingSubdividesAndExcludesUnready) {
  Summary common;
  common.intervals = 50; common.bottleneck = true;
  common.groupingVariation = 100; common.skew = -0.1; common.frequency = .2;
  Summary separate = common; separate.frequency = .5;
  Summary unready = common; unready.intervals = 49;
  auto groups = group({{"b", common}, {"c", separate}, {"a", common}, {"new", unready}});
  EXPECT_EQ(groups, (std::vector<std::vector<std::string>>{{"a", "b"}}));
  EXPECT_TRUE(group({{"a", common}}).empty());
  separate = common; separate.groupingVariation = 50;
  EXPECT_TRUE(group({{"a", common}, {"c", separate}}).empty());
  separate = common; separate.skew = -.4;
  EXPECT_TRUE(group({{"a", common}, {"c", separate}}).empty());
  separate = common; separate.loss = .3;
  EXPECT_TRUE(group({{"a", common}, {"c", separate}}).empty());
}
TEST(Sbd, CrossingsContinueWithoutCongestionAndExpireAfterFiftyIntervals) {
  Detector d;
  Summary s;
  for (int i = 0; i <= 100; ++i) {
    d.sample(i % 2 ? 100 : 0);
    s = d.finishInterval();
  }
  EXPECT_FALSE(s.bottleneck);
  EXPECT_DOUBLE_EQ(s.frequency, 49.0 / 50);
  for (int i = 0; i < 50; ++i) { d.sample(0); s = d.finishInterval(); }
  EXPECT_DOUBLE_EQ(s.frequency, 0.0);
}
TEST(Sbd, FrequencyRecountsHistoryAgainstCurrentMeanAndDeadband) {
  Detector d;
  for (int i = 0; i < 49; ++i) {
    d.sample(i % 2 ? 10 : 0);
    d.finishInterval();
  }
  // The new mean puts every old interval below it: only the last transition
  // crosses. Crossings recorded against earlier means must not survive.
  d.sample(10000);
  EXPECT_DOUBLE_EQ(d.finishInterval().frequency, 1.0 / 50);

  d.reset();
  Summary s;
  for (int i = 0; i < 50; ++i) {
    const double mean = i % 2 ? 1 : -1;
    d.sample(mean - 100); d.sample(mean + 100);
    s = d.finishInterval();
  }
  // Every interval mean is inside the current +/-20 deadband.
  EXPECT_DOUBLE_EQ(s.frequency, 0.0);
}
TEST(Sbd, HighLossReplacesSkewAndVarianceToleranceIsRelative) {
  Summary a;
  a.intervals = 50; a.bottleneck = true;
  a.loss = .3; a.skew = -.8; a.groupingVariation = 10000;
  auto b = a;
  b.loss = .8;
  EXPECT_TRUE(group({{"a", a}, {"b", b}}).empty());
  b.loss = .35; b.skew = .8;
  EXPECT_EQ(group({{"a", a}, {"b", b}}).size(), 1);
  b.loss = .25;
  EXPECT_TRUE(group({{"a", a}, {"b", b}}).empty());
  b = a; b.groupingVariation = 200000;
  EXPECT_TRUE(group({{"a", a}, {"b", b}}).empty());
  b.groupingVariation = 12000;
  EXPECT_EQ(group({{"a", a}, {"b", b}}).size(), 1);
  a.groupingVariation = 7000; b.groupingVariation = 10000;
  EXPECT_TRUE(group({{"a", a}, {"b", b}}).empty());
  a.groupingVariation = b.groupingVariation = 0;
  EXPECT_EQ(group({{"a", a}, {"b", b}}).size(), 1);
}
TEST(Sbd, MetadataDescribesActiveEstimator) {
  Service service(Config{}, "test");
  const auto json = folly::parseJson(service.json());
  EXPECT_EQ(json["algorithm"].asString(), "lcn2014-pdv2-window-v4");
  EXPECT_EQ(json["N"].asInt(), 50);
  EXPECT_EQ(json["M"].asInt(), 50);
  EXPECT_DOUBLE_EQ(json["p_v"].asDouble(), .2);
  EXPECT_DOUBLE_EQ(json["p_pdv"].asDouble(), .3);
  EXPECT_DOUBLE_EQ(json["p_l"].asDouble(), .25);
  EXPECT_DOUBLE_EQ(json["p_d"].asDouble(), .2);
  EXPECT_EQ(json["variability_estimator"].asString(), "pdv2");
  EXPECT_EQ(json["interval_clock"].asString(), "receiver_timestamp");
  EXPECT_EQ(json["feedback_grace_ms"].asInt(), 700);
  EXPECT_EQ(json["loss_interval_clock"].asString(), "packet_send");
  EXPECT_EQ(json.count("p_mad"), 0);
  EXPECT_EQ(json.count("c_h"), 0);
  Service rtt(Config{false, "rtt", ""}, "test-rtt");
  const auto rttJson = folly::parseJson(rtt.json());
  EXPECT_EQ(rttJson["interval_clock"].asString(), "ack_arrival");
  EXPECT_EQ(rttJson["feedback_grace_ms"].asInt(), 0);
  EXPECT_EQ(rttJson["feedback_timeout_ms"].asInt(), 350);
}
TEST(Sbd, RelayPeerExcludedFromClientEligibility) {
  stats::ClientNetworkMetricsStore store;
  store.sbdService = std::make_shared<Service>(Config{true, "owd", ""}, "test");
  auto flow = store.sbdService->attach("id", "peer");
  store.registerSession("id");
  store.addTrackSubscription("id", "video");
  EXPECT_TRUE(flow->eligible);
  store.markRelayPeer("id");
  EXPECT_FALSE(flow->eligible);
  store.addNamespaceSubscription("id", "more");
  EXPECT_FALSE(flow->eligible);
}

#include "sbd/VideoStart.h"
#include <moxygen/test/Mocks.h>
TEST(Sbd, VideoStartRequiresSuccessfullyForwardedPayload) {
  using namespace moxygen;
  using namespace testing;
  auto flow = std::make_shared<Flow>();
  auto consumer = std::make_shared<NiceMock<MockSubgroupConsumer>>();
  VideoSubgroup video(consumer, flow);
  EXPECT_CALL(*consumer, object(_, _, _, _))
      .WillOnce(Return(folly::makeExpected<MoQPublishError>(folly::unit)))
      .WillOnce(Return(folly::makeUnexpected(MoQPublishError(MoQPublishError::BLOCKED))))
      .WillOnce(Return(folly::makeExpected<MoQPublishError>(folly::unit)));
  EXPECT_TRUE(video.object(0, nullptr, noExtensions(), false));
  EXPECT_FALSE(flow->videoStarted);
  EXPECT_FALSE(video.object(1, folly::IOBuf::copyBuffer("frame"), noExtensions(), false));
  EXPECT_FALSE(flow->videoStarted);
  EXPECT_TRUE(video.object(2, folly::IOBuf::copyBuffer("frame"), noExtensions(), false));
  EXPECT_TRUE(flow->videoStarted);
}

TEST(Sbd, ReceiverIntervalsIgnoreAckBatchingAndBoundedReordering) {
  ReceiveIntervals immediate, batched, reordered;
  std::optional<Summary> a, b, c;
  for (int i = 0; i < 72; ++i) {
    const double delay = 1000 + 500 * std::sin(i / 3.0);
    for (int p = 0; p < 3; ++p) {
      ASSERT_TRUE(immediate.sample(i * kIntervalUs + p * 100000, delay + p * 100));
      ASSERT_TRUE(batched.sample(i * kIntervalUs + p * 100000, delay + p * 100));
    }
    // Deliver the first sample late, after the next interval has arrived.
    for (int p = 1; p < 3; ++p)
      ASSERT_TRUE(reordered.sample(i * kIntervalUs + p * 100000, delay + p * 100));
    if (i > 0)
      ASSERT_TRUE(reordered.sample((i - 1) * kIntervalUs,
                                  1000 + 500 * std::sin((i - 1) / 3.0)));
    if (auto v = immediate.finishFeedback()) a = v;
    if (i % 3 == 2) if (auto v = batched.finishFeedback()) b = v;
    if (auto v = reordered.finishFeedback()) c = v;
  }
  ASSERT_TRUE(a && b && c);
  EXPECT_TRUE(a->ready());
  for (const auto& other : {*b, *c}) {
    EXPECT_EQ(a->intervals, other.intervals);
    EXPECT_NEAR(a->skew, other.skew, 1e-12);
    EXPECT_NEAR(a->variation, other.variation, 1e-9);
    EXPECT_DOUBLE_EQ(a->frequency, other.frequency);
    EXPECT_EQ(a->samples, other.samples);
    auto x = *a, y = other;
    x.bottleneck = y.bottleneck = true;
    EXPECT_EQ(group({{"a", x}, {"b", y}}).size(), 1);
  }
}
TEST(Sbd, ReceiverIntervalsWaitForGraceAndResetOnGaps) {
  ReceiveIntervals r;
  ASSERT_TRUE(r.sample(100, 1)); // partial first bucket discarded
  ASSERT_TRUE(r.sample(kIntervalUs + 100, 2));
  ASSERT_TRUE(r.sample(3 * kIntervalUs, 3));
  EXPECT_FALSE(r.finishFeedback());
  ASSERT_TRUE(r.sample(4 * kIntervalUs, 4));
  auto s = r.finishFeedback();
  ASSERT_TRUE(s);
  EXPECT_EQ(s->intervals, 1);
  ASSERT_TRUE(r.sample(5 * kIntervalUs, 5));
  s = r.finishFeedback(); // bucket 2 was empty
  ASSERT_TRUE(s);
  EXPECT_EQ(s->intervals, 0);
  EXPECT_TRUE(s->historyReset);
  ASSERT_TRUE(r.sample(6 * kIntervalUs, 6));
  s = r.finishFeedback();
  ASSERT_TRUE(s);
  EXPECT_EQ(s->intervals, 1);
  EXPECT_FALSE(r.sample(kIntervalUs + 200, 7)); // already closed
  r.reset(); // observer invalidates history and resumes automatically
  ASSERT_TRUE(r.sample(10 * kIntervalUs, 8));
  ASSERT_TRUE(r.sample(11 * kIntervalUs, 9));
  ASSERT_TRUE(r.sample(14 * kIntervalUs, 10));
  s = r.finishFeedback();
  ASSERT_TRUE(s);
  EXPECT_EQ(s->intervals, 1);
}
TEST(Sbd, ReceiverBufferRejectsExcessAndInvalidInput) {
  ReceiveIntervals r;
  EXPECT_FALSE(r.sample(-1, 0));
  EXPECT_FALSE(r.sample(std::numeric_limits<int64_t>::max(), 0));
  EXPECT_FALSE(r.sample(0, std::numeric_limits<double>::quiet_NaN()));
  ASSERT_TRUE(r.sample(0, 0));
  EXPECT_FALSE(r.sample(100 * kIntervalUs, 0));
  for (int i = 0; i < 65536; ++i) ASSERT_TRUE(r.sample(kIntervalUs, 1));
  EXPECT_FALSE(r.sample(kIntervalUs, 1));
}
TEST(Sbd, PacketSendLossWindowExpiresAndAcceptsReorderedOutcomes) {
  FeedbackLoss loss;
  Summary s;
  s.intervals = 50;
  loss.outcomes(0, 6, 4);
  loss.apply(49 * kIntervalUs, s);
  EXPECT_EQ(s.acked, 6);
  EXPECT_EQ(s.lost, 4);
  EXPECT_DOUBLE_EQ(s.loss, .4);
  EXPECT_TRUE(s.bottleneck);
  loss.apply(50 * kIntervalUs, s);
  EXPECT_EQ(s.acked, 0);
  EXPECT_EQ(s.lost, 0);
  EXPECT_FALSE(s.bottleneck);

  loss.outcomes(100 * kIntervalUs, 9, 0);
  loss.outcomes(99 * kIntervalUs, 0, 1); // Feedback may report older sends later.
  loss.apply(100 * kIntervalUs, s);
  EXPECT_EQ(s.acked, 9);
  EXPECT_EQ(s.lost, 1);
  EXPECT_DOUBLE_EQ(s.loss, .1);
}
TEST(Sbd, ReceiverGapAutomaticallyRewarmsToReady) {
  ReceiveIntervals r;
  std::optional<Summary> latest;
  for (int i = 0; i <= 55; ++i) {
    ASSERT_TRUE(r.sample(i * kIntervalUs, i));
    if (auto s = r.finishFeedback()) latest = s;
  }
  ASSERT_TRUE(latest && latest->ready());
  // No packet in receiver interval 56. Later valid feedback first removes
  // readiness, then rebuilds a complete history without a manual reset.
  bool sawGap = false;
  for (int i = 57; i <= 110; ++i) {
    ASSERT_TRUE(r.sample(i * kIntervalUs, i));
    if (auto s = r.finishFeedback()) {
      latest = s;
      if (s->intervals == 0) sawGap = true;
    }
  }
  EXPECT_TRUE(sawGap);
  ASSERT_TRUE(latest);
  EXPECT_TRUE(latest->ready());
  EXPECT_EQ(latest->intervals, 51);
}
