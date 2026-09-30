/* SPDX-License-Identifier: Apache-2.0 */
#pragma once

#include "sbd/Service.h"

#include <quic/api/QuicSocket.h>
#include <quic/observer/SocketObserverTypes.h>

namespace openmoq::moqx::sbd {

class WeiObserver final : public quic::ManagedObserver {
 public:
  WeiObserver(quic::QuicSocket& socket, std::shared_ptr<Service> service);
  ~WeiObserver() override;

  void packetsWritten(
      quic::QuicSocketLite*,
      const PacketsWrittenEvent&) override;
  void acksProcessed(
      quic::QuicSocketLite*,
      const AcksProcessedEvent&) override;
  void packetLossDetected(
      quic::QuicSocketLite*,
      const LossEvent&) override;
  void closing(quic::QuicSocketLite*, const ClosingEvent&) noexcept override;

 private:
  std::shared_ptr<Service> service_;
  std::string id_;
  bool closed_{false};
};

} // namespace openmoq::moqx::sbd
