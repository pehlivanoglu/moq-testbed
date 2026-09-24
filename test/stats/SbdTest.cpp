/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/Detector.h"
#include "sbd/ReceiveIntervals.h"
#include "stats/ClientNetworkMetrics.h"
#include <gtest/gtest.h>
#include <folly/json.h>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <unistd.h>

using namespace openmoq::moqx;
using namespace openmoq::moqx::sbd;
namespace {
std::optional<Summary> lastSummary(std::vector<Summary> summaries) {
  if (summaries.empty()) return std::nullopt;
  return std::move(summaries.back());
}
}
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
    EXPECT_EQ(x.groupingReady(), interval >= 99);
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
TEST(Sbd, PaperSkewAveragesIntervalEstimates) {
  Detector d;
  d.sample(10); d.finishInterval();
  d.sample(20); d.finishInterval(); // interval skew -1
  for (int i = 0; i < 9; ++i) d.sample(0); // interval skew +1
  EXPECT_DOUBLE_EQ(d.finishInterval().skew, 0);
}
TEST(Sbd, MissingIntervalRestartsPaperWindow) {
  Detector d;
  for (int i = 0; i < 60; ++i) { d.sample(i); d.finishInterval(); }
  auto s = d.finishInterval();
  EXPECT_EQ(s.intervals, 0);
  EXPECT_FALSE(s.ready());
  EXPECT_FALSE(s.measurementValid);
  d.sample(3);
  s = d.finishInterval();
  EXPECT_EQ(s.intervals, 1);
  EXPECT_FALSE(s.measurementValid);
  d.sample(4);
  s = d.finishInterval();
  EXPECT_EQ(s.intervals, 2);
  EXPECT_TRUE(s.measurementValid);
  EXPECT_FALSE(s.ready());
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
  common.intervals = kGroupingWarmupIntervals; common.bottleneck = true;
  common.groupingVariation = 100; common.skew = -0.1; common.frequency = .2;
  Summary separate = common; separate.frequency = .5;
  Summary unready = common; unready.intervals = kGroupingWarmupIntervals - 1;
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
TEST(Sbd, PaperHighLossSubstitutesLossAndRfcFillsMixedCase) {
  Summary a;
  a.intervals = kGroupingWarmupIntervals; a.bottleneck = true;
  a.loss = .251; a.skew = -.1; a.groupingVariation = 10000;
  auto b = a;
  b.loss = .249; // Crossing p_l alone does not force a split.
  EXPECT_EQ(group({{"a", a}, {"b", b}}).size(), 1);
  b.loss = .1; // Mixed groups use RFC skew then relative loss.
  EXPECT_TRUE(group({{"a", a}, {"b", b}}).empty());

  a.loss = .3; a.skew = -.8; b = a; b.loss = .32; b.skew = .8;
  EXPECT_EQ(group({{"a", a}, {"b", b}}).size(), 1);
  b.loss = .35;
  EXPECT_TRUE(group({{"a", a}, {"b", b}}).empty());
}
TEST(Sbd, GroupingVariationToleranceIsRelative) {
  Summary a;
  a.intervals = kGroupingWarmupIntervals; a.bottleneck = true;
  a.skew = -.1; a.groupingVariation = 10000;
  auto b = a;
  b.groupingVariation = 200000;
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
  EXPECT_EQ(json["algorithm"].asString(), "lcn2014-pdv2-rfc-fill-v10");
  EXPECT_EQ(json["N"].asInt(), 50);
  EXPECT_EQ(json["M"].asInt(), 50);
  EXPECT_DOUBLE_EQ(json["p_v"].asDouble(), .2);
  EXPECT_DOUBLE_EQ(json["p_pdv"].asDouble(), .3);
  EXPECT_DOUBLE_EQ(json["p_l"].asDouble(), .25);
  EXPECT_DOUBLE_EQ(json["p_d"].asDouble(), .1);
  EXPECT_EQ(json["loss_threshold_mode"].asString(), "paper_substitution_rfc_relative_to_larger");
  EXPECT_EQ(json["metric_window_intervals"].asInt(), 50);
  EXPECT_EQ(json["grouping_warmup_intervals"].asInt(), 100);
  EXPECT_EQ(json["parameter_precedence"].asString(), "LCN2014_then_RFC8382");
  EXPECT_EQ(json["gap_policy"].asString(), "reset_and_rewarm");
  EXPECT_EQ(json["group_interval_policy"].asString(), "latest_common_completed_interval");
  EXPECT_EQ(json["transport_gap_policy"].asString(), "stale_without_reset");
  EXPECT_EQ(json["loss_correction"].asString(), "packet_number_ledger");
  EXPECT_EQ(json["variability_estimator"].asString(), "pdv2");
  EXPECT_EQ(json["interval_clock"].asString(), "receiver_clock_monotonic");
  EXPECT_EQ(json["delay_measurement"].asString(), "absolute_owd");
  EXPECT_EQ(json["receive_timestamp_basis"].asString(), "linux_clock_monotonic");
  EXPECT_TRUE(json.count("snapshot_unix_ns"));
  EXPECT_TRUE(json.count("group_decision_mono_us"));
  EXPECT_EQ(json["feedback_grace_ms"].asInt(), 0);
  EXPECT_EQ(json["interval_completion"].asString(), "receive_timestamp_watermark");
  EXPECT_EQ(json["loss_interval_clock"].asString(), "packet_send");
  EXPECT_EQ(json.count("p_mad"), 0);
  EXPECT_EQ(json.count("c_h"), 0);
  Service rtt(Config{false, "rtt", ""}, "test-rtt");
  const auto rttJson = folly::parseJson(rtt.json());
  EXPECT_EQ(rttJson["interval_clock"].asString(), "ack_arrival");
  EXPECT_EQ(rttJson["group_interval_policy"].asString(), "latest_fresh_summary");
  EXPECT_EQ(rttJson["feedback_grace_ms"].asInt(), 0);
  EXPECT_EQ(rttJson["interval_completion"].asString(), "ack_arrival_timer");
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
  for (int i = 0; i < 122; ++i) {
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
    const auto watermark = i * kIntervalUs + 200000;
    if (auto v = lastSummary(immediate.finishThrough(watermark))) a = v;
    if (i % 3 == 2)
      if (auto v = lastSummary(batched.finishThrough(watermark))) b = v;
    if (auto v = lastSummary(reordered.finishThrough(watermark))) c = v;
  }
  // Interval 121's deliberately delayed first sample would arrive in batch 122.
  // Complete only through interval 120 so all three traces cover equal data.
  const auto watermark = 121 * kIntervalUs;
  if (auto v = lastSummary(immediate.finishThrough(watermark))) a = v;
  if (auto v = lastSummary(batched.finishThrough(watermark))) b = v;
  if (auto v = lastSummary(reordered.finishThrough(watermark))) c = v;
  ASSERT_TRUE(a && b && c);
  EXPECT_TRUE(a->ready());
  EXPECT_TRUE(a->groupingReady());
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
TEST(Sbd, ReceiverIntervalsFinishAtWatermarkAndRestartAcrossGaps) {
  ReceiveIntervals r;
  ASSERT_TRUE(r.sample(100, 1)); // partial first bucket discarded
  ASSERT_TRUE(r.sample(kIntervalUs + 100, 2));
  EXPECT_TRUE(r.finishThrough(2 * kIntervalUs - 1).empty());
  auto s = lastSummary(r.finishThrough(2 * kIntervalUs));
  ASSERT_TRUE(s);
  EXPECT_EQ(s->intervals, 1);
  EXPECT_EQ(s->intervalStartUs, kIntervalUs);
  EXPECT_EQ(s->intervalEndUs, 2 * kIntervalUs);
  EXPECT_DOUBLE_EQ(s->intervalMeanDelayUs, 2);
  EXPECT_DOUBLE_EQ(s->intervalMinDelayUs, 2);
  EXPECT_DOUBLE_EQ(s->intervalMaxDelayUs, 2);
  ASSERT_TRUE(r.sample(3 * kIntervalUs, 3));
  s = lastSummary(r.finishThrough(3 * kIntervalUs)); // bucket 2 was empty
  ASSERT_TRUE(s);
  EXPECT_EQ(s->intervals, 0);
  EXPECT_FALSE(s->measurementValid);
  ASSERT_TRUE(r.sample(4 * kIntervalUs, 4));
  s = lastSummary(r.finishThrough(4 * kIntervalUs));
  ASSERT_TRUE(s);
  EXPECT_EQ(s->intervals, 1);
  EXPECT_FALSE(s->measurementValid);
  ASSERT_TRUE(r.sample(5 * kIntervalUs, 5));
  s = lastSummary(r.finishThrough(5 * kIntervalUs));
  ASSERT_TRUE(s);
  EXPECT_EQ(s->intervals, 2);
  EXPECT_TRUE(s->measurementValid);
  EXPECT_FALSE(r.sample(kIntervalUs + 200, 7)); // already closed
  r.reset(); // observer invalidates history and resumes automatically
  ASSERT_TRUE(r.sample(10 * kIntervalUs, 8));
  ASSERT_TRUE(r.sample(11 * kIntervalUs, 9));
  ASSERT_TRUE(r.sample(14 * kIntervalUs, 10));
  s = lastSummary(r.finishThrough(12 * kIntervalUs));
  ASSERT_TRUE(s);
  EXPECT_EQ(s->intervals, 1);
  EXPECT_TRUE(r.finishThrough(11 * kIntervalUs).empty()); // stale watermark is harmless
}

TEST(Sbd, ArchivesExactGroupTransitionTime) {
  char path[] = "/tmp/moqx-sbd-XXXXXX";
  const int fd = mkstemp(path);
  ASSERT_GE(fd, 0);
  close(fd);
  std::filesystem::remove(path);
  {
    Service service(Config{true, "owd", path}, "test");
    Summary summary;
    summary.intervals = kGroupingWarmupIntervals;
    summary.bottleneck = true;
    summary.measurementValid = true;
    summary.groupingVariation = 100;
    summary.skew = -0.1;
    summary.intervalStartUs = 10 * kIntervalUs;
    summary.intervalEndUs = 11 * kIntervalUs;
    for (const auto* id : {"a", "b"}) {
      service.attach(id, "peer");
      service.eligible(id, true);
      service.publish(id, summary, "ready");
    }
  }
  std::ifstream input(std::string(path) + ".events.jsonl");
  std::string line;
  bool sawDetection = false, sawGroup = false;
  while (std::getline(input, line)) {
    const auto event = folly::parseJson(line);
    if (event["event"] == "bottleneck_state_changed") {
      sawDetection = true;
      EXPECT_TRUE(event["bottleneck"].asBool());
      EXPECT_GT(event["detected_unix_ns"].asInt(), 0);
      EXPECT_EQ(event["interval_end_mono_us"].asInt(), 11 * kIntervalUs);
    } else if (event["event"] == "groups_changed") {
      sawGroup = true;
      EXPECT_GT(event["decision_unix_ns"].asInt(), 0);
      EXPECT_GT(event["decision_mono_us"].asInt(), 0);
      EXPECT_EQ(event["interval_end_mono_us"].asInt(), 11 * kIntervalUs);
      EXPECT_EQ(event["groups"][0].size(), 2);
    }
  }
  EXPECT_TRUE(sawDetection);
  EXPECT_TRUE(sawGroup);
  std::filesystem::remove(path);
  std::filesystem::remove(std::string(path) + ".events.jsonl");
  std::filesystem::remove(std::string(path) + ".latest.json");
}
TEST(Sbd, DoesNotGroupDifferentReceiverIntervals) {
  char path[] = "/tmp/moqx-sbd-epochs-XXXXXX";
  const int fd = mkstemp(path);
  ASSERT_GE(fd, 0);
  close(fd);
  std::filesystem::remove(path);
  {
    Service service(Config{true, "owd", path}, "test");
    Summary summary;
    summary.intervals = kGroupingWarmupIntervals;
    summary.bottleneck = summary.measurementValid = true;
    summary.groupingVariation = 100;
    summary.skew = -0.1;
    for (const auto* id : {"a", "b"}) {
      service.attach(id, "peer");
      service.eligible(id, true);
      summary.intervalStartUs = (id[0] == 'a' ? 10 : 9) * kIntervalUs;
      summary.intervalEndUs = summary.intervalStartUs + kIntervalUs;
      service.publish(id, summary, "ready");
    }
  }
  std::ifstream input(path);
  std::string line, last;
  while (std::getline(input, line)) last = line;
  const auto snapshot = folly::parseJson(last);
  EXPECT_TRUE(snapshot["groups"].empty());
  EXPECT_EQ(snapshot["group_interval_end_mono_us"].asInt(), 0);
  std::filesystem::remove(path);
  std::filesystem::remove(std::string(path) + ".events.jsonl");
  std::filesystem::remove(std::string(path) + ".latest.json");
}
TEST(Sbd, WaitingStatusPreservesDecisionWithoutFalseTransition) {
  char path[] = "/tmp/moqx-sbd-status-XXXXXX";
  const int fd = mkstemp(path);
  ASSERT_GE(fd, 0);
  close(fd);
  std::filesystem::remove(path);
  {
    Service service(Config{true, "owd", path}, "test");
    service.attach("a", "peer");
    service.eligible("a", true);
    Summary summary;
    summary.intervals = kGroupingWarmupIntervals;
    summary.measurementValid = true;
    summary.bottleneck = true;
    service.publish("a", summary, "ready");
    service.status("a", "waiting_feedback");
  }
  std::ifstream snapshots(path);
  std::string line, last;
  while (std::getline(snapshots, line)) last = line;
  const auto snapshot = folly::parseJson(last);
  ASSERT_EQ(snapshot["clients"].size(), 1);
  const auto& client = snapshot["clients"][0];
  EXPECT_EQ(client["status"].asString(), "waiting_feedback");
  EXPECT_FALSE(client["decision_valid"].asBool());
  EXPECT_TRUE(client["bottleneck"].asBool()); // Last valid value; decision is unknown.
  EXPECT_EQ(client["intervals"].asInt(), kGroupingWarmupIntervals);

  std::ifstream events(std::string(path) + ".events.jsonl");
  size_t transitions = 0;
  while (std::getline(events, line))
    if (folly::parseJson(line)["event"] == "bottleneck_state_changed") ++transitions;
  EXPECT_EQ(transitions, 1);
  std::filesystem::remove(path);
  std::filesystem::remove(std::string(path) + ".events.jsonl");
  std::filesystem::remove(std::string(path) + ".latest.json");
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
  s.measurementValid = true;
  for (uint64_t packet = 0; packet < 6; ++packet)
    ASSERT_TRUE(loss.acknowledged(0, packet));
  for (uint64_t packet = 6; packet < 10; ++packet)
    ASSERT_TRUE(loss.declaredLost(0, packet));
  loss.apply(50 * kIntervalUs, s);
  EXPECT_EQ(s.acked, 6);
  EXPECT_EQ(s.lost, 4);
  EXPECT_DOUBLE_EQ(s.loss, .4);
  EXPECT_TRUE(s.bottleneck);
  loss.apply(51 * kIntervalUs, s);
  EXPECT_EQ(s.acked, 0);
  EXPECT_EQ(s.lost, 0);
  EXPECT_FALSE(s.bottleneck);

  for (uint64_t packet = 10; packet < 19; ++packet)
    ASSERT_TRUE(loss.acknowledged(100 * kIntervalUs, packet));
  ASSERT_TRUE(loss.declaredLost(99 * kIntervalUs, 19)); // Older sends may arrive later.
  loss.apply(101 * kIntervalUs, s);
  EXPECT_EQ(s.acked, 9);
  EXPECT_EQ(s.lost, 1);
  EXPECT_DOUBLE_EQ(s.loss, .1);
}
TEST(Sbd, SpuriousLossReplacesProvisionalLossExactlyOnce) {
  FeedbackLoss loss;
  Summary s;
  s.intervals = 50;
  s.measurementValid = true;
  ASSERT_TRUE(loss.declaredLost(0, 1));
  ASSERT_TRUE(loss.declaredLost(0, 1));
  loss.apply(kIntervalUs, s);
  EXPECT_EQ(s.acked, 0);
  EXPECT_EQ(s.lost, 1);
  ASSERT_TRUE(loss.spuriousLoss(0, 1));
  ASSERT_TRUE(loss.spuriousLoss(0, 1));
  loss.apply(kIntervalUs, s);
  EXPECT_EQ(s.acked, 1);
  EXPECT_EQ(s.lost, 0);
}
TEST(Sbd, ReceiverGapRequiresPaperWindowRewarm) {
  ReceiveIntervals r;
  std::optional<Summary> latest;
  for (int i = 0; i <= 55; ++i) {
    ASSERT_TRUE(r.sample(i * kIntervalUs, i));
    if (auto s = lastSummary(r.finishThrough(i * kIntervalUs))) latest = s;
  }
  ASSERT_TRUE(latest && latest->ready());
  // No packet in receiver interval 56. The empty slot and the next interval
  // cannot decide skew, then the immediately following interval can.
  bool sawGap = false;
  int firstValidAfterGap = -1;
  for (int i = 57; i <= 110; ++i) {
    ASSERT_TRUE(r.sample(i * kIntervalUs, i));
    if (auto s = lastSummary(r.finishThrough(i * kIntervalUs))) {
      latest = s;
      if (!s->measurementValid) sawGap = true;
      else if (sawGap && firstValidAfterGap < 0) firstValidAfterGap = i;
    }
  }
  EXPECT_TRUE(sawGap);
  ASSERT_TRUE(latest);
  EXPECT_TRUE(latest->ready());
  EXPECT_EQ(firstValidAfterGap, 59);
  EXPECT_EQ(latest->intervals, 53);
}
