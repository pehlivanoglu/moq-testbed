/* SPDX-License-Identifier: Apache-2.0 */
#pragma once
#include "sbd/Detector.h"
#include <map>
#include <optional>

namespace openmoq::moqx::sbd {
inline constexpr int64_t kIntervalUs = 350000;
inline constexpr auto kFeedbackTimeout = 5 * kInterval;

// Receiver-time buckets. The caller supplies a watermark only after processing
// every timestamp in one cumulative ACK feedback batch.
class ReceiveIntervals {
 public:
  bool sample(int64_t receiveUs, double delayUs);
  std::optional<Summary> finishThrough(int64_t receiveWatermarkUs);
  void reset() { *this = ReceiveIntervals{}; }
 private:
  Detector detector_;
  std::map<int64_t, std::vector<double>> buckets_;
  std::optional<int64_t> next_, first_;
  int64_t watermark_{0};
  size_t samples_{0};
};

// Lost packets have no receive timestamp. Bucket transport outcomes by their
// original send time so ACK batching does not move them between loss windows.
class FeedbackLoss {
 public:
  bool acknowledged(int64_t sentUs, uint64_t packetNum);
  bool declaredLost(int64_t sentUs, uint64_t packetNum);
  bool spuriousLoss(int64_t sentUs, uint64_t packetNum);
  void apply(int64_t latestSentUs, Summary& summary);
  void reset() { *this = FeedbackLoss{}; }
 private:
  struct Entry { int64_t interval; uint64_t acked, lost; };
  enum class Outcome { Acked, Lost };
  struct Packet { int64_t interval; Outcome outcome; };
  bool outcome(int64_t sentUs, uint64_t packetNum, Outcome value);
  void expire(int64_t interval);
  std::map<int64_t, Entry> history_;
  std::map<uint64_t, Packet> packets_;
  std::optional<int64_t> latestInterval_;
};
} // namespace openmoq::moqx::sbd
