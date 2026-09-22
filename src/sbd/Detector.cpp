/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/Detector.h"
#include <algorithm>
#include <cmath>

namespace openmoq::moqx::sbd {
void Detector::sample(double delayUs) {
  ++count_;
  sum_ += delayUs;
  minDelay_ = count_ == 1 ? delayUs : std::min(minDelay_, delayUs);
  maxDelay_ = count_ == 1 ? delayUs : std::max(maxDelay_, delayUs);
  if (size_) {
    skewBase_ += delayUs < mean_ ? 1 : delayUs > mean_ ? -1 : 0;
  }
}
void Detector::outcomes(uint64_t acked, uint64_t lost) {
  acked_ += acked;
  lost_ += lost;
}
void Detector::reset() { *this = Detector{}; }
Summary Detector::finishInterval() {
  if (!count_) {
    reset();
    Summary result;
    result.historyReset = true;
    return result;
  }
  double currentMean = sum_ / count_;
  Interval current{count_, acked_, lost_, currentMean,
                   size_ ? skewBase_ / count_ : 0, 
                   maxDelay_ - currentMean, // PDV2
                   0, true, false};
  auto at = [&](size_t age) -> Interval& {
    return history_[(cursor_ + kN - age) % kN];
  };
  history_[cursor_] = current;
  size_ = std::min(size_ + 1, kN);
  Summary result;
  result.intervals = ++intervals_;
  result.samples = count_;
  result.intervalMeanDelayUs = currentMean;
  result.intervalMinDelayUs = minDelay_;
  result.intervalMaxDelayUs = maxDelay_;
  
  double skew = 0, variation = 0, mean = 0;
  uint64_t acked = 0, lost = 0;
  size_t meanSize = std::min(size_, kM);
  for (size_t age = 0; age < meanSize; ++age) {
    const auto& h = at(age);
    skew += h.skewBase;
    variation += h.varBase;
    mean += h.mean;
  }
  for (size_t age = 0; age < size_; ++age) {
    acked += at(age).acked;
    lost += at(age).lost;
  }
  if (meanSize) {
    skew /= meanSize;
    variation /= meanSize;
    mean /= meanSize;
  }
  result.skew = skew;
  result.variation = variation;
  at(0).variation = variation;
  result.groupingVariation = variation; // Unused differently in paper

  const auto outcomes = acked + lost;
  result.acked = acked;
  result.lost = lost;
  result.loss = outcomes ? double(lost) / outcomes : 0;
  
  // Bottleneck detection (strict < 0.0 or high loss)
  result.bottleneck = current.validBase && (result.skew < 0.0 || result.loss > kLossThreshold);
  at(0).validVar = result.bottleneck;

  const double threshold = kCrossingThreshold * result.variation;
  // Recount oldest to newest against today's mean and deadband (paper IV-C).
  // The first significant point establishes a side; it is not a crossing.
  int previousSide = 0;
  for (size_t age = size_; age > 0; --age) {
    const double intervalMean = at(age - 1).mean;
    const int side = intervalMean > mean + threshold ? 1 :
                     intervalMean < mean - threshold ? -1 : 0;
    if (side) {
      result.frequency += previousSide && side != previousSide;
      previousSide = side;
    }
  }
  result.frequency /= size_;
  
  mean_ = currentMean;
  cursor_ = (cursor_ + 1) % kN;
  count_ = acked_ = lost_ = 0;
  sum_ = skewBase_ = minDelay_ = maxDelay_ = 0;
  return result;
}

std::vector<std::vector<std::string>> group(std::vector<FlowSummary> flows) {
  std::erase_if(flows, [](const auto& f) {
    return !f.metrics.groupingReady() || !f.metrics.bottleneck;
  });
  std::vector<std::vector<FlowSummary>> groups;
  if (!flows.empty()) groups.push_back(std::move(flows));
  auto split = [&](auto metric, auto together) {
    std::vector<std::vector<FlowSummary>> next;
    for (auto& g : groups) {
      std::sort(g.begin(), g.end(), [&](const auto& a, const auto& b) {
        const double av = metric(a.metrics), bv = metric(b.metrics);
        return av == bv ? a.id < b.id : av > bv;
      });
      for (size_t i = 0; i < g.size(); ++i) {
        if (!i || !together(metric(g[i - 1].metrics), metric(g[i].metrics)))
          next.emplace_back();
        next.back().push_back(g[i]);
      }
    }
    groups = std::move(next);
  };
  
  split([](const auto& m) { return m.frequency; }, [](double a, double b) { return std::abs(a - b) < kFrequencyThreshold; });
  // RFC 8382 section 3.3.1 supplies the relative form omitted by the paper.
  split([](const auto& m) { return m.groupingVariation; }, [](double a, double b) {
    return a == b || std::abs(a - b) < kVariationThreshold * std::max(a, b);
  });
  // Keep the two final-stage metrics in separate groups. High loss replaces
  // skewness (LCN 2014 section V-B).
  split([](const auto& m) { return m.loss > kLossThreshold ? 1.0 : 0.0; },
        [](double a, double b) { return a == b; });
  split([](const auto& m) { return m.loss > kLossThreshold ? 0.0 : m.skew; },
        [](double a, double b) { return std::abs(a - b) < kSkewThreshold; });
  split([](const auto& m) { return m.loss > kLossThreshold ? m.loss : 0.0; },
        [](double a, double b) { return std::abs(a - b) < kLossDifferenceThreshold; });
  
  std::vector<std::vector<std::string>> result;
  for (const auto& g : groups) {
    if (g.size() < 2) continue;
    auto& ids = result.emplace_back();
    for (const auto& f : g) ids.push_back(f.id);
    std::sort(ids.begin(), ids.end());
  }
  std::sort(result.begin(), result.end());
  return result;
}
} // namespace openmoq::moqx::sbd
