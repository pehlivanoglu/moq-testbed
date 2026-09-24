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
  return true;
}
std::vector<Summary> ReceiveIntervals::finishThrough(int64_t receiveWatermarkUs) {
  std::vector<Summary> result;
  if (!next_ || receiveWatermarkUs < 0) return result;
  watermark_ = std::max(watermark_, receiveWatermarkUs);
  while ((*next_ + 1) * kIntervalUs <= watermark_) {
    if (auto it = buckets_.find(*next_); it != buckets_.end()) {
      for (double delay : it->second) detector_.sample(delay);
      samples_ -= it->second.size();
      buckets_.erase(it);
    }
    // The paper assumes a usable estimate every T. An empty T invalidates the
    // contiguous evidence window and restarts warm-up instead of adding zero.
    auto summary = detector_.finishInterval();
    summary.intervalStartUs = *next_ * kIntervalUs;
    summary.intervalEndUs = (*next_ + 1) * kIntervalUs;
    result.push_back(std::move(summary));
    ++*next_;
  }
  return result;
}
void FeedbackLoss::expire(int64_t interval) {
  latestInterval_ = std::max(latestInterval_.value_or(interval), interval);
  // Keep enough history to finalize delayed receiver-time intervals.
  const auto cutoff = *latestInterval_ - int64_t(2 * kN);
  while (!history_.empty() && history_.begin()->first <= cutoff)
    history_.erase(history_.begin());
  while (!packets_.empty() && packets_.begin()->second.interval <= cutoff)
    packets_.erase(packets_.begin());
}
bool FeedbackLoss::outcome(int64_t sentUs, uint64_t packetNum, Outcome value) {
  const auto interval = sentUs / kIntervalUs;
  expire(interval);
  if (interval <= *latestInterval_ - int64_t(2 * kN)) return true;
  if (auto found = packets_.find(packetNum); found != packets_.end()) {
    if (found->second.outcome == value || found->second.outcome == Outcome::Acked)
      return true;
    auto history = history_.find(found->second.interval);
    if (history != history_.end() && history->second.lost) {
      --history->second.lost;
      ++history->second.acked;
    }
    found->second.outcome = Outcome::Acked;
    return true;
  }
  if (packets_.size() >= 65536) return false;
  packets_.emplace(packetNum, Packet{interval, value});
  auto it = history_.try_emplace(interval, Entry{interval, 0, 0}).first;
  if (value == Outcome::Acked) ++it->second.acked;
  else ++it->second.lost;
  return true;
}
bool FeedbackLoss::acknowledged(int64_t sentUs, uint64_t packetNum) {
  return outcome(sentUs, packetNum, Outcome::Acked);
}
bool FeedbackLoss::declaredLost(int64_t sentUs, uint64_t packetNum) {
  return outcome(sentUs, packetNum, Outcome::Lost);
}
bool FeedbackLoss::spuriousLoss(int64_t sentUs, uint64_t packetNum) {
  return outcome(sentUs, packetNum, Outcome::Acked);
}
void FeedbackLoss::apply(int64_t windowEndUs, Summary& summary) {
  const auto end = windowEndUs / kIntervalUs;
  expire(end);
  summary.acked = summary.lost = 0;
  for (const auto& [_, entry] : history_) {
    if (entry.interval < end - int64_t(kN) || entry.interval >= end) continue;
    summary.acked += entry.acked;
    summary.lost += entry.lost;
  }
  const auto total = summary.acked + summary.lost;
  summary.loss = total ? double(summary.lost) / total : 0;
  summary.bottleneck = summary.measurementValid &&
      (summary.skew < 0 || summary.loss > kLossThreshold);
}
} // namespace openmoq::moqx::sbd
