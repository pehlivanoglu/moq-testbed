/* SPDX-License-Identifier: Apache-2.0 */
#pragma once

#include "sbd/Detector.h"
#include <array>
#include <map>

namespace openmoq::moqx::sbd {
inline constexpr std::string_view kDcSbd{"DcSBD"};
inline constexpr size_t kDcN = 50, kDcM = 30, kDcF = 20;
inline constexpr size_t kDcFullHistoryIntervals = 2 * kDcN;
inline constexpr double kDcCongestionSkew = 0.1;
inline constexpr double kDcHysteresisSkew = 0.3;
inline constexpr double kDcLossThreshold = 0.25;
inline constexpr double kDcFrequencyDifference = 0.1;
inline constexpr double kDcMadDifference = 0.1;
inline constexpr double kDcSkewDifference = 0.15;
inline constexpr double kDcLossDifference = 0.1;
inline constexpr double kDcCrossingThreshold = 0.7;

class DcEstimator {
 public:
  void sample(double delayUs);
  Summary finishInterval();
  void reset();

 private:
  struct Interval {
    uint64_t count{0};
    double mean{0}, skewBase{0}, varBase{0};
  };
  std::array<Interval, kDcN> history_{};
  std::vector<double> samples_;
  size_t cursor_{0}, size_{0};
  uint64_t intervals_{0};
};

bool dcBottleneck(const Summary& summary, bool previouslyBottlenecked);

// The paper leaves normalization and the "too distant" cutoff unspecified.
// Differences are expressed in units of their RFC grouping thresholds; a
// squared distance greater than one is considered too distant.
double dcDistanceSquared(const Summary& a, const Summary& b);

class DcClusterer {
 public:
  std::vector<std::vector<std::string>> update(
      std::vector<FlowSummary> flows);
  void remove(const std::string& id) { classes_.erase(id); }
  void reset() { classes_.clear(); nextClass_ = 1; }

 private:
  std::map<std::string, uint64_t> classes_;
  uint64_t nextClass_{1};
};
} // namespace openmoq::moqx::sbd
