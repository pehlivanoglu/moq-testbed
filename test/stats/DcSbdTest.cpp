/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/DcSbd.h"
#include "sbd/ReceiveIntervals.h"
#include "sbd/Service.h"

#include <folly/json.h>
#include <gtest/gtest.h>

using namespace openmoq::moqx::sbd;

namespace {
Summary metric(double skew = 0, double variation = 100, double frequency = .2,
               double loss = .01) {
  Summary result;
  result.intervals = kDcM;
  result.readyAfter = result.groupingReadyAfter = kDcM;
  result.fullHistoryAfter = kDcFullHistoryIntervals;
  result.measurementValid = true;
  result.bottleneck = true;
  result.skew = skew;
  result.variation = result.groupingVariation = variation;
  result.frequency = frequency;
  result.loss = loss;
  return result;
}
}

TEST(DcSbd, WeightedSummaryUsesPaperWindows) {
  DcEstimator detector;
  detector.sample(10);
  auto summary = detector.finishInterval();
  EXPECT_EQ(summary.intervals, 1);
  EXPECT_FALSE(summary.measurementValid);

  detector.sample(10);
  detector.sample(30);
  summary = detector.finishInterval();
  EXPECT_TRUE(summary.measurementValid);
  EXPECT_NEAR(summary.skew, -1.0 / 3.0, 1e-12);
  EXPECT_NEAR(summary.variation, 20.0 / 3.0, 1e-12);

  for (size_t interval = 2; interval < kDcFullHistoryIntervals; ++interval) {
    detector.sample(interval % 2 ? 10 : 30);
    summary = detector.finishInterval();
    EXPECT_EQ(summary.ready(), interval + 1 >= kDcM);
    EXPECT_EQ(summary.groupingReady(), interval + 1 >= kDcM);
    EXPECT_EQ(summary.fullHistory(), interval + 1 >= kDcFullHistoryIntervals);
  }
}

TEST(DcSbd, MissingIntervalResetsAndRewarms) {
  ReceiveIntervals intervals(true);
  ASSERT_TRUE(intervals.sample(100, 1));
  ASSERT_TRUE(intervals.sample(kIntervalUs + 100, 2));
  auto summaries = intervals.finishThrough(2 * kIntervalUs);
  ASSERT_EQ(summaries.size(), 1);
  EXPECT_EQ(summaries.back().readyAfter, kDcM);
  EXPECT_EQ(summaries.back().intervals, 1);

  ASSERT_TRUE(intervals.sample(3 * kIntervalUs + 100, 3));
  summaries = intervals.finishThrough(3 * kIntervalUs);
  ASSERT_EQ(summaries.size(), 1);
  EXPECT_EQ(summaries.back().intervals, 0);
  EXPECT_FALSE(summaries.back().ready());
}

TEST(DcSbd, CongestionClassificationHasPaperHysteresis) {
  auto summary = metric(.09);
  EXPECT_TRUE(dcBottleneck(summary, false));
  summary.skew = .2;
  EXPECT_FALSE(dcBottleneck(summary, false));
  EXPECT_TRUE(dcBottleneck(summary, true));
  summary.skew = .31;
  EXPECT_FALSE(dcBottleneck(summary, true));
  summary.loss = .251;
  EXPECT_TRUE(dcBottleneck(summary, false));
}

TEST(DcSbd, CrossingsAreNotGatedByBottleneckClassification) {
  DcEstimator detector;
  Summary summary;
  for (size_t interval = 0; interval < kDcN; ++interval) {
    detector.sample(interval % 2 ? 1 : 0);
    summary = detector.finishInterval();
  }
  EXPECT_DOUBLE_EQ(summary.frequency, 49.0 / 50);
}

TEST(DcSbd, DistanceUsesThresholdNormalizedFourDimensionalMeasure) {
  const auto a = metric();
  auto b = a;
  b.skew += kDcSkewDifference;
  EXPECT_DOUBLE_EQ(dcDistanceSquared(a, b), 1);
  b = a;
  b.frequency += kDcFrequencyDifference / 2;
  b.variation = 100.0 / .95;
  EXPECT_NEAR(dcDistanceSquared(a, b), .5, 1e-12);
}

TEST(DcSbd, DynamicClusteringReusesThenSeparatesClasses) {
  DcClusterer clusterer;
  auto a = metric(), b = metric(), c = metric(.5);
  EXPECT_EQ(clusterer.update({{"c", c}, {"b", b}, {"a", a}}),
            (std::vector<std::vector<std::string>>{{"a", "b"}}));

  b.skew = .3;
  EXPECT_TRUE(clusterer.update({{"a", a}, {"b", b}, {"c", c}}).empty());

  b = a;
  EXPECT_EQ(clusterer.update({{"b", b}, {"a", a}}),
            (std::vector<std::vector<std::string>>{{"a", "b"}}));
  a.bottleneck = false;
  EXPECT_TRUE(clusterer.update({{"a", a}, {"b", b}}).empty());
}

TEST(DcSbd, MetadataRecordsParametersAndInterpretations) {
  Service service(Config{false, "owd", "", std::string(kDcSbd)}, "test");
  const auto json = folly::parseJson(service.json());
  EXPECT_EQ(json["algorithm"].asString(), kDcSbd);
  EXPECT_EQ(json["N"].asInt(), 50);
  EXPECT_EQ(json["M"].asInt(), 30);
  EXPECT_EQ(json["F"].asInt(), 20);
  EXPECT_EQ(json["grouping_warmup_intervals"].asInt(), 30);
  EXPECT_EQ(json["full_history_intervals"].asInt(), 100);
  EXPECT_DOUBLE_EQ(json["c_s"].asDouble(), .1);
  EXPECT_DOUBLE_EQ(json["c_h"].asDouble(), .3);
  EXPECT_DOUBLE_EQ(json["p_mad"].asDouble(), .1);
  EXPECT_DOUBLE_EQ(json["p_v"].asDouble(), .7);
  EXPECT_DOUBLE_EQ(json["p_l"].asDouble(), .25);
  EXPECT_EQ(json["variability_estimator"].asString(), "mad");
  EXPECT_EQ(json["distance_measure"].asString(),
            "inverse_sum_of_squared_normalized_differences");
  EXPECT_EQ(json["clustering_iterations"].asInt(), 2);
  EXPECT_EQ(json["processing_order"].asString(),
            "connection_id_with_recently_separated_last");
  EXPECT_FALSE(json["crossing_bottleneck_gating"].asBool());
}
