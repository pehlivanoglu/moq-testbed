/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/WeiObserver.h"

#include "sbd/WeiDetector.h"

namespace openmoq::moqx::sbd {

WeiObserver::WeiObserver(
    quic::QuicSocket& socket,
    std::shared_ptr<Service> service)
    : quic::ManagedObserver(
          EventSetBuilder()
              .enable(Events::packetsWrittenEvents)
              .enable(Events::acksProcessedEvents)
              .enable(Events::lossEvents)
              .build()),
      service_(std::move(service)) {
  if (auto cid = socket.getServerConnectionId()) {
    id_ = cid->hex();
  } else if (auto cid = socket.getClientConnectionId()) {
    id_ = cid->hex();
  }
  if (!id_.empty()) {
    service_->attach(id_, socket.getPeerAddress().describe());
  }
}

WeiObserver::~WeiObserver() {
  if (!closed_ && !id_.empty()) {
    service_->close(id_);
  }
}

void WeiObserver::packetsWritten(
    quic::QuicSocketLite* socket,
    const PacketsWrittenEvent& event) {
  auto* detector = service_->weiDetector();
  if (!detector || id_.empty()) {
    return;
  }
  std::vector<uint64_t> packetNumbers;
  event.invokeForEachNewOutstandingPacketOrdered([&](const auto& packet) {
    if (packet.packet.header.getPacketNumberSpace() ==
        quic::PacketNumberSpace::AppData) {
      packetNumbers.push_back(packet.getPacketSequenceNum());
    }
  });
  const auto info = socket->getTransportInfo();
  detector->packetsWritten(id_, packetNumbers, info.congestionWindow, info.mss);
}

void WeiObserver::acksProcessed(
    quic::QuicSocketLite* socket,
    const AcksProcessedEvent& event) {
  auto* detector = service_->weiDetector();
  if (!detector || id_.empty()) {
    return;
  }
  const auto info = socket->getTransportInfo();
  for (const auto& ack : event.ackEvents) {
    if (ack.implicit || ack.packetNumberSpace != quic::PacketNumberSpace::AppData) {
      continue;
    }
    std::vector<uint64_t> packetNumbers;
    packetNumbers.reserve(ack.ackedPackets.size());
    for (const auto& packet : ack.ackedPackets) {
      packetNumbers.push_back(packet.packetNum);
    }
    const uint64_t cwnd = ack.ccState
        ? ack.ccState->congestionWindowBytes
        : info.congestionWindow;
    detector->ackEcn(
        id_,
        ack.ackTime,
        packetNumbers,
        ack.ecnECT0Count,
        ack.ecnECT1Count,
        ack.ecnCECount,
        cwnd,
        info.mss);
  }
}

void WeiObserver::packetLossDetected(
    quic::QuicSocketLite* socket,
    const LossEvent& event) {
  auto* detector = service_->weiDetector();
  if (!detector || id_.empty()) {
    return;
  }
  std::vector<uint64_t> packetNumbers;
  for (const auto& packet : event.lostPackets) {
    if (packet.pnSpace == quic::PacketNumberSpace::AppData) {
      packetNumbers.push_back(packet.packetNum);
    }
  }
  const auto info = socket->getTransportInfo();
  detector->loss(
      id_, event.lossTime, packetNumbers, info.congestionWindow, info.mss);
}

void WeiObserver::closing(
    quic::QuicSocketLite*,
    const ClosingEvent&) noexcept {
  closed_ = true;
  if (!id_.empty()) {
    service_->close(id_);
  }
}

} // namespace openmoq::moqx::sbd
