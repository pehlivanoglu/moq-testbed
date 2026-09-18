/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/ReceiveIntervals.h"
#include <algorithm>
#include <cmath>
#include <limits>

namespace openmoq::moqx::sbd {
bool ReceiveIntervals::sample(int64_t receiveUs, double delayUs) {
  if (receiveUs < 0 || receiveUs > std::numeric_limits<int64_t>::max() - 10 * kIntervalUs ||
      !std::isfinite(delayUs)) return false;
  const auto bucket = receiveUs / kIntervalUs;
  if (!next_) {
    first_ = bucket;
    next_ = bucket + 1; // Discard the partial interval at measurement start.
  }
  if (bucket == *first_) return true;
  // Late feedback invalidates already published statistics. Bound both sample
  // storage and processing after a large timestamp jump or long feedback batch.
  if (bucket < *next_ || bucket - *next_ > 8 || samples_ >= 65536) return false;
  buckets_[bucket].push_back(delayUs);
  ++samples_;
  newest_ = std::max(newest_, receiveUs);
  return true;
}
std::optional<Summary> ReceiveIntervals::finishFeedback() {
  std::optional<Summary> result;
  bool historyReset = false;
  if (!next_) return result;
  const auto watermark = newest_ - kFeedbackGraceUs;
  while ((*next_ + 1) * kIntervalUs <= watermark) {
    if (auto it = buckets_.find(*next_); it != buckets_.end()) {
      for (double delay : it->second) detector_.sample(delay);
      samples_ -= it->second.size();
      buckets_.erase(it);
    }
    // An empty receiver interval resets readiness; the next valid interval
    // starts a new history automatically, without synthesizing delay samples.
    result = detector_.finishInterval();
    historyReset |= result->historyReset;
    ++*next_;
  }
  if (result) result->historyReset = historyReset;
  return result;
}
void FeedbackLoss::expire(int64_t interval) {
  while (!history_.empty() && history_.begin()->first <= interval - int64_t(kN))
    history_.erase(history_.begin());
}
void FeedbackLoss::outcomes(int64_t sentUs, uint64_t acked, uint64_t lost) {
  const auto interval = sentUs / kIntervalUs;
  auto it = history_.try_emplace(interval, Entry{interval, 0, 0}).first;
  it->second.acked += acked;
  it->second.lost += lost;
}
void FeedbackLoss::apply(int64_t latestSentUs, Summary& summary) {
  expire(latestSentUs / kIntervalUs);
  summary.acked = summary.lost = 0;
  for (const auto& [_, entry] : history_) {
    summary.acked += entry.acked;
    summary.lost += entry.lost;
  }
  const auto total = summary.acked + summary.lost;
  summary.loss = total ? double(summary.lost) / total : 0;
  summary.bottleneck = summary.intervals &&
      (summary.skew < 0 || summary.loss > kLossThreshold);
}
} // namespace openmoq::moqx::sbd
