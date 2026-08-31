/*
 * Copyright (c) OpenMOQ contributors.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "stats/ClientNetworkMetrics.h"

#include <algorithm>

namespace openmoq::moqx::stats {

namespace {
constexpr auto kPublishInterval = std::chrono::milliseconds(100);
}

void ClientNetworkMetricsStore::put(ClientNetworkMetrics metrics) {
  std::lock_guard lock(mutex_);
  clients_.insert_or_assign(metrics.connectionId, std::move(metrics));
}

std::vector<ClientNetworkMetrics> ClientNetworkMetricsStore::snapshot() const {
  std::lock_guard lock(mutex_);
  std::vector<ClientNetworkMetrics> result;
  result.reserve(clients_.size());
  for (const auto& [_, metrics] : clients_) {
    result.push_back(metrics);
  }
  std::sort(result.begin(), result.end(), [](const auto& a, const auto& b) {
    return a.connectionId < b.connectionId;
  });
  return result;
}

ClientNetworkMetricsObserver::ClientNetworkMetricsObserver(
    quic::QuicSocket& socket,
    std::shared_ptr<ClientNetworkMetricsStore> store)
    : quic::ManagedObserver(
          EventSetBuilder()
              .enable(Events::acksProcessedEvents)
              .enable(Events::rttSamples)
              .enable(Events::lossEvents)
              .enable(Events::l4sWeightUpdatedEvents)
              .enable(Events::pacingRateUpdatedEvents)
              .enable(Events::appRateLimitedEvents)
              .build()),
      store_(std::move(store)) {
  if (auto cid = socket.getServerConnectionId()) {
    metrics_.connectionId = cid->hex();
  } else if (auto cid = socket.getClientConnectionId()) {
    metrics_.connectionId = cid->hex();
  } else {
    metrics_.connectionId = socket.getPeerAddress().describe();
  }
  metrics_.peer = socket.getPeerAddress().describe();
  readTransportInfo(socket);
  publish(true);
}

void ClientNetworkMetricsObserver::readTransportInfo(quic::QuicSocketLite& socket) {
  const auto info = socket.getTransportInfo();
  metrics_.srttUs = info.srtt.count();
  metrics_.rttVarUs = info.rttvar.count();
  metrics_.minRttUs = info.maybeMinRtt ? info.maybeMinRtt->count() : 0;
  metrics_.queueDelayUs =
      metrics_.srttUs > metrics_.minRttUs ? metrics_.srttUs - metrics_.minRttUs : 0;
  metrics_.cwndBytes = info.congestionWindow;
  metrics_.inflightBytes = info.bytesInFlight;
  metrics_.writableBytes = info.writableBytes;
  metrics_.retransmittedPackets = info.packetsRetransmitted;
  currentBytesAcked_ = info.bytesAcked;
}

void ClientNetworkMetricsObserver::publish(bool force) {
  const auto now = std::chrono::steady_clock::now();
  if (!force && now - lastPublish_ < kPublishInterval) {
    return;
  }
  if (lastPublish_ != std::chrono::steady_clock::time_point{}) {
    const auto elapsedUs =
        std::chrono::duration_cast<std::chrono::microseconds>(now - lastPublish_).count();
    if (elapsedUs > 0 && currentBytesAcked_ >= lastRateBytesAcked_) {
      metrics_.ackedRateBps =
          (currentBytesAcked_ - lastRateBytesAcked_) * 8'000'000ULL / elapsedUs;
    }
  }
  const uint64_t ecn = metrics_.ect0 + metrics_.ect1 + metrics_.ce;
  const uint64_t newEcn = ecn >= lastRateEcn_ ? ecn - lastRateEcn_ : 0;
  const uint64_t newCe = metrics_.ce >= lastRateCe_ ? metrics_.ce - lastRateCe_ : 0;
  metrics_.ceFraction = newEcn == 0 ? 0.0 : static_cast<double>(newCe) / newEcn;
  metrics_.updatedAt = now;
  store_->put(metrics_);
  lastRateBytesAcked_ = currentBytesAcked_;
  lastRateEcn_ = ecn;
  lastRateCe_ = metrics_.ce;
  lastPublish_ = now;
}

void ClientNetworkMetricsObserver::acksProcessed(
    quic::QuicSocketLite* socket,
    const AcksProcessedEvent& event) {
  for (const auto& ack : event.ackEvents) {
    metrics_.ect0 = std::max(metrics_.ect0, ack.ecnECT0Count);
    metrics_.ect1 = std::max(metrics_.ect1, ack.ecnECT1Count);
    metrics_.ce = std::max(metrics_.ce, ack.ecnCECount);
  }
  metrics_.ecnCapable = metrics_.ect0 + metrics_.ect1 + metrics_.ce > 0;
  readTransportInfo(*socket);
  publish();
}

void ClientNetworkMetricsObserver::packetLossDetected(
    quic::QuicSocketLite*,
    const LossEvent& event) {
  metrics_.lostPackets += event.lostPackets.size();
  publish();
}

void ClientNetworkMetricsObserver::rttSampleGenerated(
    quic::QuicSocketLite* socket,
    const PacketRTT&) {
  readTransportInfo(*socket);
  publish();
}

void ClientNetworkMetricsObserver::l4sWeightUpdated(
    quic::QuicSocketLite*,
    const L4sWeightUpdateEvent& event) noexcept {
  metrics_.l4sWeight = event.l4sWeight;
  publish();
}

void ClientNetworkMetricsObserver::pacingRateUpdated(
    quic::QuicSocketLite* socket,
    const PacingRateUpdateEvent& event) noexcept {
  const auto intervalUs = event.interval.count();
  if (intervalUs > 0) {
    metrics_.pacingRateBps =
        event.packetsPerInterval * socket->getTransportInfo().mss * 8'000'000ULL / intervalUs;
  }
  publish();
}

void ClientNetworkMetricsObserver::appRateLimited(
    quic::QuicSocketLite*,
    const AppLimitedEvent&) {
  metrics_.appLimited = true;
  publish();
}

void ClientNetworkMetricsObserver::startWritingFromAppLimited(
    quic::QuicSocketLite*,
    const AppLimitedEvent&) {
  metrics_.appLimited = false;
  publish();
}

void ClientNetworkMetricsObserver::closing(
    quic::QuicSocketLite* socket,
    const ClosingEvent&) noexcept {
  readTransportInfo(*socket);
  metrics_.active = false;
  publish(true);
}

} // namespace openmoq::moqx::stats
