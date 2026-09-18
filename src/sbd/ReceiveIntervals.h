/* SPDX-License-Identifier: Apache-2.0 */
#pragma once
#include "sbd/Detector.h"
#include <map>
#include <optional>

namespace openmoq::moqx::sbd {
inline constexpr int64_t kIntervalUs = 350000;
inline constexpr int64_t kFeedbackGraceUs = 2 * kIntervalUs;
inline constexpr auto kFeedbackTimeout = 5 * kInterval;

// Receiver-time buckets. Finalize only after later receiver timestamps have
// advanced beyond the grace period; wall time cannot prove completeness.
class ReceiveIntervals {
 public:
  bool sample(int64_t receiveUs, double delayUs);
  std::optional<Summary> finishFeedback();
  void reset() { *this = ReceiveIntervals{}; }
 private:
  Detector detector_;
  std::map<int64_t, std::vector<double>> buckets_;
  std::optional<int64_t> next_, first_;
  int64_t newest_{0};
  size_t samples_{0};
};

// Lost packets have no receive timestamp. Bucket transport outcomes by their
// original send time so ACK batching does not move them between loss windows.
class FeedbackLoss {
 public:
  void outcomes(int64_t sentUs, uint64_t acked, uint64_t lost);
  void apply(int64_t latestSentUs, Summary& summary);
  void reset() { history_.clear(); }
 private:
  struct Entry { int64_t interval; uint64_t acked, lost; };
  void expire(int64_t interval);
  std::map<int64_t, Entry> history_;
};
} // namespace openmoq::moqx::sbd
