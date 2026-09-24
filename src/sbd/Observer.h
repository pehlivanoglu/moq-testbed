/* SPDX-License-Identifier: Apache-2.0 */
#pragma once
#include "sbd/Service.h"
#include "sbd/ReceiveIntervals.h"
#include <folly/io/async/AsyncTimeout.h>
#include <quic/api/QuicSocket.h>
#include <quic/observer/SocketObserverTypes.h>

namespace openmoq::moqx::sbd {
class Observer final : public quic::ManagedObserver, public folly::AsyncTimeout {
 public:
  Observer(quic::QuicSocket& socket, std::shared_ptr<Service> service);
  ~Observer() override;
  void acksProcessed(quic::QuicSocketLite*, const AcksProcessedEvent&) override;
  void packetLossDetected(quic::QuicSocketLite*, const LossEvent&) override;
  void spuriousLossDetected(quic::QuicSocketLite*, const SpuriousLossEvent&) override;
  void closing(quic::QuicSocketLite*, const ClosingEvent&) noexcept override;
  void timeoutExpired() noexcept override;
 private:
  bool measuring();
  void stop(std::string reason);
  void recover(std::string reason);
  std::shared_ptr<Service> service_;
  std::shared_ptr<Flow> flow_;
  std::string id_, peer_;
  folly::SocketAddress peerAddress_;
  Detector detector_;
  ReceiveIntervals receiver_;
  FeedbackLoss loss_;
  bool started_{false}, stopped_{false}, closed_{false};
  uint64_t samples_{0};
  std::chrono::steady_clock::time_point next_{}, measurementStart_{};
  std::chrono::steady_clock::time_point lastFeedback_{};
};
} // namespace openmoq::moqx::sbd
