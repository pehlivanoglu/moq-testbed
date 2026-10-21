/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/DcSbd.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <set>

namespace openmoq::moqx::sbd {
namespace {
size_t weight(size_t age) {
  return age < kDcF ? kDcM - kDcF + 1 : kDcM - age;
}

double relativeDifference(double a, double b, double threshold) {
  const double high = std::max(std::abs(a), std::abs(b));
  if (high == 0) return 0;
  return std::abs(a - b) / (threshold * high);
}
}

void DcEstimator::sample(double delayUs) {
  samples_.push_back(delayUs);
}

void DcEstimator::reset() {
  *this = DcEstimator{};
}

Summary DcEstimator::finishInterval() {
  if (samples_.empty()) {
    reset();
    return {};
  }

  auto at = [&](size_t age) -> Interval& {
    return history_[(cursor_ + kDcN - age) % kDcN];
  };
  const size_t previousMeans = std::min(size_, kDcM);
  double previousMean = 0;
  for (size_t age = 1; age <= previousMeans; ++age)
    previousMean += at(age).mean;
  if (previousMeans) previousMean /= previousMeans;

  double sum = 0;
  for (double delay : samples_) sum += delay;
  const double currentMean = sum / samples_.size();
  Interval current;
  current.count = samples_.size();
  current.mean = currentMean;
  for (double delay : samples_) {
    if (previousMeans)
      current.skewBase += delay < previousMean ? 1 : delay > previousMean ? -1 : 0;
    current.varBase += std::abs(delay - currentMean);
  }

  history_[cursor_] = current;
  size_ = std::min(size_ + 1, kDcN);

  Summary result;
  result.intervals = ++intervals_;
  result.samples = samples_.size();
  result.readyAfter = result.groupingReadyAfter = kDcM;
  result.fullHistoryAfter = kDcFullHistoryIntervals;
  result.intervalMeanDelayUs = currentMean;
  result.intervalMinDelayUs = *std::min_element(samples_.begin(), samples_.end());
  result.intervalMaxDelayUs = *std::max_element(samples_.begin(), samples_.end());
  result.measurementValid = previousMeans != 0;

  double weightedSkew = 0, weightedVariation = 0, weightedSamples = 0;
  const size_t metricSize = std::min(size_, kDcM);
  for (size_t age = 0; age < metricSize; ++age) {
    const auto& interval = at(age);
    const double w = weight(age);
    weightedSkew += w * interval.skewBase;
    weightedVariation += w * interval.varBase;
    weightedSamples += w * interval.count;
  }
  if (weightedSamples) {
    result.skew = weightedSkew / weightedSamples;
    result.variation = weightedVariation / weightedSamples;
  }
  result.groupingVariation = result.variation;

  double mean = 0;
  const size_t meanSize = std::min(size_, kDcM);
  for (size_t age = 0; age < meanSize; ++age) mean += at(age).mean;
  if (meanSize) mean /= meanSize;
  const double deadband = kDcCrossingThreshold * result.variation;
  int previousSide = 0;
  for (size_t age = size_; age > 0; --age) {
    const double intervalMean = at(age - 1).mean;
    const int side = intervalMean > mean + deadband ? 1 :
        intervalMean < mean - deadband ? -1 : 0;
    if (side) {
      result.frequency += previousSide && side != previousSide;
      previousSide = side;
    }
  }
  result.frequency /= kDcN;

  cursor_ = (cursor_ + 1) % kDcN;
  samples_.clear();
  return result;
}

bool dcBottleneck(const Summary& summary, bool previouslyBottlenecked) {
  return summary.ready() && summary.measurementValid &&
      (summary.skew < kDcCongestionSkew ||
       (previouslyBottlenecked && summary.skew < kDcHysteresisSkew) ||
       summary.loss > kDcLossThreshold);
}

double dcDistanceSquared(const Summary& a, const Summary& b) {
  const double skew = std::abs(a.skew - b.skew) / kDcSkewDifference;
  const double frequency =
      std::abs(a.frequency - b.frequency) / kDcFrequencyDifference;
  const double variation =
      relativeDifference(a.variation, b.variation, kDcMadDifference);
  const double loss = relativeDifference(a.loss, b.loss, kDcLossDifference);
  return skew * skew + frequency * frequency +
      variation * variation + loss * loss;
}

std::vector<std::vector<std::string>> DcClusterer::update(
    std::vector<FlowSummary> flows) {
  std::sort(flows.begin(), flows.end(), [](const auto& a, const auto& b) {
    return a.id < b.id;
  });
  const std::set<std::string> active = [&] {
    std::set<std::string> ids;
    for (const auto& flow : flows) ids.insert(flow.id);
    return ids;
  }();
  std::erase_if(classes_, [&](const auto& item) {
    return !active.contains(item.first);
  });

  std::vector<const FlowSummary*> bottlenecked;
  for (const auto& flow : flows) {
    if (!flow.metrics.groupingReady() || !flow.metrics.bottleneck) {
      classes_[flow.id] = 0;
      continue;
    }
    if (!classes_[flow.id]) classes_[flow.id] = nextClass_++;
    bottlenecked.push_back(&flow);
  }

  std::map<uint64_t, std::vector<const FlowSummary*>> previousClusters;
  for (const auto* flow : bottlenecked)
    previousClusters[classes_.at(flow->id)].push_back(flow);

  std::set<std::string> separated;
  for (const auto& [_, cluster] : previousClusters) {
    if (cluster.size() < 2) continue;
    const FlowSummary* furthest = nullptr;
    double furthestNearest = -1;
    for (const auto* flow : cluster) {
      double nearest = std::numeric_limits<double>::infinity();
      for (const auto* other : cluster)
        if (flow != other)
          nearest = std::min(nearest, dcDistanceSquared(flow->metrics, other->metrics));
      if (nearest > furthestNearest) {
        furthest = flow;
        furthestNearest = nearest;
      }
    }
    if (furthest && furthestNearest > 1) {
      classes_[furthest->id] = nextClass_++;
      separated.insert(furthest->id);
    }
  }

  std::stable_partition(bottlenecked.begin(), bottlenecked.end(),
      [&](const auto* flow) { return !separated.contains(flow->id); });
  for (size_t iteration = 0; iteration < 2; ++iteration) {
    for (const auto* flow : bottlenecked) {
      const FlowSummary* closest = nullptr;
      double closestDistance = std::numeric_limits<double>::infinity();
      for (const auto* other : bottlenecked) {
        if (flow == other) continue;
        const double distance = dcDistanceSquared(flow->metrics, other->metrics);
        if (distance < closestDistance) {
          closest = other;
          closestDistance = distance;
        }
      }
      if (closest && closestDistance <= 1)
        classes_[flow->id] = classes_.at(closest->id);
    }
  }

  std::map<uint64_t, std::vector<std::string>> byClass;
  for (const auto* flow : bottlenecked)
    byClass[classes_.at(flow->id)].push_back(flow->id);
  std::vector<std::vector<std::string>> result;
  for (auto& [_, group] : byClass) {
    if (group.size() < 2) continue;
    std::sort(group.begin(), group.end());
    result.push_back(std::move(group));
  }
  std::sort(result.begin(), result.end());
  return result;
}
} // namespace openmoq::moqx::sbd
