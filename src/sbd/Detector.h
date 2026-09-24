/* SPDX-License-Identifier: Apache-2.0 */
#pragma once

#include <array>
#include <chrono>
#include <cstdint>
#include <string>
#include <vector>

namespace openmoq::moqx::sbd {
inline constexpr auto kInterval = std::chrono::milliseconds(350);
inline constexpr size_t kN = 50, kM = 50;
inline constexpr size_t kGroupingWarmupIntervals = 2 * kM;
inline constexpr double kLossThreshold = 0.25;
inline constexpr double kFrequencyThreshold = 0.2, kVariationThreshold = 0.3;
inline constexpr double kSkewThreshold = 0.2, kLossDifferenceThreshold = 0.2;
inline constexpr double kCrossingThreshold = 0.2;

struct Summary {
  uint64_t intervals{0}, samples{0}, acked{0}, lost{0};
  double skew{0}, variation{0}, frequency{0}, loss{0}, groupingVariation{0};
  double intervalMeanDelayUs{0}, intervalMinDelayUs{0}, intervalMaxDelayUs{0};
  int64_t intervalStartUs{0}, intervalEndUs{0};
  bool bottleneck{false};
  bool measurementValid{false};
  bool ready() const { return intervals >= kN; }
  bool groupingReady() const { return intervals >= kGroupingWarmupIntervals; }
};

// Single connection, owned by its transport EventBase. No per-packet allocation.
class Detector {
 public:
  void sample(double delayUs);
  void outcomes(uint64_t acked, uint64_t lost);
  Summary finishInterval();
  void reset();
 private:
  struct Interval {
    uint64_t count{0}, acked{0}, lost{0};
    double mean{0}, skewBase{0}, varBase{0}, variation{0};
    bool validBase{false}, validVar{false};
  };
  std::array<Interval, kN> history_{};
  size_t cursor_{0}, size_{0};
  uint64_t intervals_{0}, count_{0}, acked_{0}, lost_{0};
  double sum_{0}, skewBase_{0}, minDelay_{0}, maxDelay_{0}, mean_{0};
  bool previousMeanValid_{false};
};

struct FlowSummary {
  std::string id;
  Summary metrics;
};
// Input must contain ready, fresh flows. Non-bottleneck flows are excluded.
std::vector<std::vector<std::string>> group(std::vector<FlowSummary> flows);
} // namespace openmoq::moqx::sbd
