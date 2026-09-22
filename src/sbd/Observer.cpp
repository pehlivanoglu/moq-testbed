/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/Observer.h"
#include <folly/io/async/EventBaseManager.h>
#include <folly/logging/xlog.h>
#include <algorithm>

namespace openmoq::moqx::sbd {
namespace {
int64_t clockUs(std::chrono::steady_clock::time_point time) {
  return std::chrono::duration_cast<std::chrono::microseconds>(
      time.time_since_epoch()).count();
}
}

Observer::Observer(quic::QuicSocket& socket, std::shared_ptr<Service> service)
    : quic::ManagedObserver(EventSetBuilder().enable(Events::acksProcessedEvents)
          .enable(Events::lossEvents).enable(Events::spuriousLossEvents).build()),
      folly::AsyncTimeout(folly::EventBaseManager::get()->getEventBase()),
      service_(std::move(service)), peer_(socket.getPeerAddress().describe()), peerAddress_(socket.getPeerAddress()) {
  if (auto cid = socket.getServerConnectionId()) id_ = cid->hex();
  else if (auto cid = socket.getClientConnectionId()) id_ = cid->hex();
  else { stopped_ = true; return; }
  flow_ = service_->attach(id_, peer_);
  scheduleTimeout(kInterval);
}
Observer::~Observer() { cancelTimeout(); if (!closed_) service_->close(id_); }
bool Observer::measuring() {
  if (closed_ || stopped_ || !flow_) return false;
  if (!flow_->eligible || !flow_->videoStarted) {
    if (started_) {
      XLOG(INFO) << "SBD connection=" << id_ << " history_reset reason=measurement_ineligible";
      detector_.reset(); receiver_.reset(); loss_.reset(); started_ = false;
      samples_ = 0; latestSentUs_ = 0;
    }
    return false;
  }
  if (!started_) {
    started_ = true;
    measurementStart_ = lastFeedback_ = std::chrono::steady_clock::now();
    next_ = measurementStart_ + kInterval;
    scheduleTimeout(kInterval);
    service_->publish(id_, {}, "warming_up");
  }
  return true;
}
void Observer::stop(std::string reason) {
  XLOG(WARN) << "SBD connection=" << id_ << " history_reset terminal=true reason=" << reason;
  stopped_ = true;
  detector_.reset();
  receiver_.reset();
  loss_.reset();
  latestSentUs_ = 0;
  cancelTimeout();
  service_->publish(id_, {}, std::move(reason));
}
void Observer::recover(std::string reason) {
  XLOG(WARN) << "SBD connection=" << id_ << " history_reset reason=" << reason;
  detector_.reset();
  receiver_.reset();
  loss_.reset();
  samples_ = 0;
  latestSentUs_ = 0;
  measurementStart_ = lastFeedback_ = std::chrono::steady_clock::now();
  next_ = measurementStart_ + kInterval;
  service_->publish(id_, {}, std::move(reason));
  scheduleTimeout(kInterval);
}
void Observer::acksProcessed(quic::QuicSocketLite* socket, const AcksProcessedEvent& event) {
  if (!measuring()) return;
  if (socket->getPeerAddress() != peerAddress_) { stop("stopped_path_changed"); return; }
  const bool owd = service_->config().delaySource == "owd";
  // RTT retains feedback-time intervals. OWD is finalized only by receiver time.
  if (!owd && std::chrono::steady_clock::now() >= next_) timeoutExpired();
  std::vector<std::pair<int64_t, std::chrono::steady_clock::time_point>> received;
  uint64_t measuredOutcomes = 0;
  for (const auto& ack : event.ackEvents) {
    if (ack.implicit || ack.packetNumberSpace != quic::PacketNumberSpace::AppData) continue;
    for (const auto& packet : ack.ackedPackets) {
      if (packet.outstandingPacketMetadata.time < measurementStart_) continue;
      ++measuredOutcomes;
      const auto sentUs = clockUs(packet.outstandingPacketMetadata.time);
      latestSentUs_ = std::max(latestSentUs_, sentUs);
      loss_.outcomes(sentUs, 1, 0);
      if (owd) {
        if (!packet.receiveRelativeTimeStampUsec) {
          recover("waiting_receive_timestamps");
          return;
        }
        const auto rx = packet.receiveRelativeTimeStampUsec->count();
        if (rx < 0) { recover("waiting_valid_timestamp"); return; }
        if (received.size() >= 65536) { recover("waiting_feedback_capacity"); return; }
        received.emplace_back(rx, packet.outstandingPacketMetadata.time);
      }
    }
    if (!owd) {
      const auto* packet = ack.getLargestAckedPacket();
      if (packet && packet->outstandingPacketMetadata.time >= measurementStart_ &&
          ack.rttSampleNoAckDelay && ack.rttSampleNoAckDelay->count() >= 0) {
        detector_.sample(ack.rttSampleNoAckDelay->count());
        ++samples_;
      }
    }
  }
  if (measuredOutcomes) lastFeedback_ = std::chrono::steady_clock::now();
  // ACK packet order need not be receive order, including the first batch.
  std::sort(received.begin(), received.end());
  for (const auto& [rx, sent] : received) {
    const auto tx = clockUs(sent);
    const double delay = double(rx - tx);
    if (delay < 0) {
      recover("waiting_shared_monotonic_clock");
      return;
    }
    if (!receiver_.sample(rx, delay)) {
      recover("waiting_late_or_excess_feedback");
      return;
    }
  }
  if (owd) {
    if (auto summary = receiver_.finishFeedback()) {
      if (summary->historyReset)
        XLOG(WARN) << "SBD connection=" << id_ << " history_reset reason=empty_receiver_interval";
      loss_.apply(latestSentUs_, *summary);
      service_->publish(id_, *summary, summary->ready() ? "ready" :
          summary->intervals ? "warming_up" : "waiting_receiver_samples");
    }
  }
}

void Observer::packetLossDetected(quic::QuicSocketLite*, const LossEvent& event) {
  if (!measuring()) return;
  for (const auto& packet : event.lostPackets) {
    if (packet.pnSpace != quic::PacketNumberSpace::AppData ||
        packet.packetMetadata.time < measurementStart_) continue;
    const auto sentUs = clockUs(packet.packetMetadata.time);
    latestSentUs_ = std::max(latestSentUs_, sentUs);
    loss_.outcomes(sentUs, 0, 1);
  }
}

void Observer::spuriousLossDetected(quic::QuicSocketLite*, const SpuriousLossEvent&) {
  // Sender-declared loss is provisional. Do not silently double-count a packet
  // as both lost and received when the transport reports a correction.
  if (measuring()) recover("waiting_after_spurious_loss");
}
void Observer::timeoutExpired() noexcept {
  if (closed_ || stopped_) return;
  if (!measuring()) { scheduleTimeout(kInterval); return; }
  const auto now = std::chrono::steady_clock::now();
  if (service_->config().delaySource == "owd") {
    if (now - lastFeedback_ >= kFeedbackTimeout) recover("waiting_feedback");
    else scheduleTimeout(kInterval);
    return;
  }
  if (now >= next_) {
    if (now - next_ >= kInterval) { recover("waiting_after_timer_lag"); return; }
    if (!samples_) { recover("waiting_feedback"); return; }
    auto summary = detector_.finishInterval();
    summary.intervalStartUs = clockUs(next_ - kInterval);
    summary.intervalEndUs = clockUs(next_);
    loss_.apply(latestSentUs_, summary);
    service_->publish(id_, summary, summary.ready() ? "ready" : "warming_up");
    samples_ = 0;
    next_ += kInterval;
  }
  const auto delay = std::chrono::duration_cast<std::chrono::milliseconds>(next_ - now);
  scheduleTimeout(std::max(delay, std::chrono::milliseconds(1)));
}
void Observer::closing(quic::QuicSocketLite*, const ClosingEvent&) noexcept {
  closed_ = true;
  cancelTimeout();
  service_->close(id_);
}
} // namespace openmoq::moqx::sbd
