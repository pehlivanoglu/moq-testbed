/*
 * Copyright (c) OpenMOQ contributors.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "stats/ClientNetworkMetrics.h"

#include <algorithm>
#include <limits>

namespace openmoq::moqx::stats {

namespace {
constexpr auto kPublishInterval = std::chrono::milliseconds(100);

uint64_t delta(uint64_t current, uint64_t previous, bool& reset) {
  if (current < previous) {
    reset = true;
    return 0;
  }
  return current - previous;
}

int64_t trend(uint64_t current, uint64_t previous) {
  if (current >= previous) {
    return static_cast<int64_t>(std::min<uint64_t>(
        current - previous,
        std::numeric_limits<int64_t>::max()));
  }
  return -static_cast<int64_t>(std::min<uint64_t>(
      previous - current,
      static_cast<uint64_t>(std::numeric_limits<int64_t>::max())));
}
}

ClientNetworkMetricsStore::ClientNetworkMetricsStore(
    std::chrono::milliseconds historyWindow,
    std::chrono::milliseconds sampleInterval,
    std::chrono::milliseconds inactiveRetention)
    : historyWindow_(historyWindow),
      sampleInterval_(sampleInterval),
      inactiveRetention_(inactiveRetention) {}

void ClientNetworkMetricsStore::put(ClientNetworkMetrics metrics) {
  std::lock_guard lock(mutex_);
  if (metrics.updatedAt == std::chrono::steady_clock::time_point{}) {
    metrics.updatedAt = std::chrono::steady_clock::now();
  }

  auto& history = histories_[metrics.connectionId];
  const bool clockReset =
      !history.empty() && metrics.updatedAt < history.back().metrics.updatedAt;
  if (clockReset) {
    history.clear();
  }
  const bool due = history.empty() || !metrics.active ||
      metrics.updatedAt - history.back().metrics.updatedAt >= sampleInterval_;
  if (due) {
    HistorySample sample{metrics};
    if (!history.empty()) {
      const auto& previous = history.back().metrics;
      sample.newAckedPackets =
          delta(metrics.ackedPackets, previous.ackedPackets, sample.counterReset);
      sample.newEct0 = delta(metrics.ect0, previous.ect0, sample.counterReset);
      sample.newEct1 = delta(metrics.ect1, previous.ect1, sample.counterReset);
      sample.newCe = delta(metrics.ce, previous.ce, sample.counterReset);
      sample.newLostPackets =
          delta(metrics.lostPackets, previous.lostPackets, sample.counterReset);
      sample.newRetransmittedPackets = delta(
          metrics.retransmittedPackets,
          previous.retransmittedPackets,
          sample.counterReset);
    }
    history.push_back(std::move(sample));

    const auto cutoff = metrics.updatedAt - historyWindow_;
    bool removed = false;
    while (!history.empty() && history.front().metrics.updatedAt < cutoff) {
      history.pop_front();
      removed = true;
    }
    if (removed && !history.empty()) {
      auto& first = history.front();
      first.newAckedPackets = 0;
      first.newEct0 = 0;
      first.newEct1 = 0;
      first.newCe = 0;
      first.newLostPackets = 0;
      first.newRetransmittedPackets = 0;
      first.counterReset = false;
    }
  }
  clients_.insert_or_assign(metrics.connectionId, std::move(metrics));
}

std::vector<ClientNetworkMetrics> ClientNetworkMetricsStore::snapshot() const {
  std::lock_guard lock(mutex_);
  const auto now = std::chrono::steady_clock::now();
  std::vector<ClientNetworkMetrics> result;
  result.reserve(clients_.size());
  for (const auto& [_, metrics] : clients_) {
    auto copy = metrics;
    if (auto historyIt = histories_.find(metrics.connectionId);
        historyIt != histories_.end()) {
      auto& history = historyIt->second;
      const auto cutoff = now - historyWindow_;
      bool removed = false;
      while (!history.empty() && history.front().metrics.updatedAt < cutoff) {
        history.pop_front();
        removed = true;
      }
      if (!metrics.active && now - metrics.updatedAt > inactiveRetention_) {
        history.clear();
      } else if (removed && !history.empty()) {
        auto& first = history.front();
        first.newAckedPackets = 0;
        first.newEct0 = 0;
        first.newEct1 = 0;
        first.newCe = 0;
        first.newLostPackets = 0;
        first.newRetransmittedPackets = 0;
        first.counterReset = false;
      }

      copy.windowSamples = history.size();
      if (!history.empty()) {
        const auto& first = history.front().metrics;
        const auto& last = history.back().metrics;
        copy.windowDurationMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                                    last.updatedAt - first.updatedAt)
                                    .count();
        copy.windowQueueDelayTrendUs = trend(last.queueDelayUs, first.queueDelayUs);
        copy.windowSrttTrendUs = trend(last.srttUs, first.srttUs);
        copy.windowAckedRateTrendBps = trend(last.ackedRateBps, first.ackedRateBps);
        copy.windowCwndTrendBytes = trend(last.cwndBytes, first.cwndBytes);
        uint64_t blocked = 0;
        uint64_t appLimited = 0;
        for (const auto& sample : history) {
          copy.windowAckedPackets += sample.newAckedPackets;
          copy.windowEct0 += sample.newEct0;
          copy.windowEct1 += sample.newEct1;
          copy.windowCe += sample.newCe;
          copy.windowLostPackets += sample.newLostPackets;
          copy.windowRetransmittedPackets += sample.newRetransmittedPackets;
          copy.windowCounterReset |= sample.counterReset;
          blocked += sample.metrics.writableBytes == 0;
          appLimited += sample.metrics.appLimited;
        }
        const auto windowEcn = copy.windowEct0 + copy.windowEct1 + copy.windowCe;
        copy.windowCeFraction = windowEcn == 0
            ? 0.0
            : static_cast<double>(copy.windowCe) / windowEcn;
        const auto packetOutcomes = copy.windowAckedPackets + copy.windowLostPackets;
        copy.windowLossRate = packetOutcomes == 0
            ? 0.0
            : static_cast<double>(copy.windowLostPackets) / packetOutcomes;
        copy.windowWritableBlockedFraction =
            static_cast<double>(blocked) / history.size();
        copy.windowAppLimitedFraction =
            static_cast<double>(appLimited) / history.size();
      }
    }
    if (auto it = sessions_.find(metrics.connectionId); it != sessions_.end()) {
      const auto& session = it->second;
      copy.sessionMapped = true;
      copy.publishedTracks.assign(session.publishedTracks.begin(), session.publishedTracks.end());
      copy.publishedNamespaces.assign(
          session.publishedNamespaces.begin(),
          session.publishedNamespaces.end());
      copy.trackSubscriptions.assign(
          session.trackSubscriptions.begin(),
          session.trackSubscriptions.end());
      copy.namespaceSubscriptions.assign(
          session.namespaceSubscriptions.begin(),
          session.namespaceSubscriptions.end());
    }
    result.push_back(std::move(copy));
  }
  std::sort(result.begin(), result.end(), [](const auto& a, const auto& b) {
    return a.connectionId < b.connectionId;
  });
  return result;
}

void ClientNetworkMetricsStore::registerSession(std::string connectionId) {
  if (connectionId.empty()) {
    return;
  }
  std::lock_guard lock(mutex_);
  sessions_.insert_or_assign(std::move(connectionId), SessionMetadata{});
}

void ClientNetworkMetricsStore::unregisterSession(std::string_view connectionId) {
  std::lock_guard lock(mutex_);
  sessions_.erase(std::string(connectionId));
}

void ClientNetworkMetricsStore::addPublishedTrack(
    std::string_view connectionId,
    std::string track) {
  std::lock_guard lock(mutex_);
  if (auto it = sessions_.find(std::string(connectionId)); it != sessions_.end()) {
    it->second.publishedTracks.insert(std::move(track));
  }
}

void ClientNetworkMetricsStore::removePublishedTrack(
    std::string_view connectionId,
    std::string_view track) {
  std::lock_guard lock(mutex_);
  if (auto it = sessions_.find(std::string(connectionId)); it != sessions_.end()) {
    it->second.publishedTracks.erase(std::string(track));
  }
}

void ClientNetworkMetricsStore::addPublishedNamespace(
    std::string_view connectionId,
    std::string trackNamespace) {
  std::lock_guard lock(mutex_);
  if (auto it = sessions_.find(std::string(connectionId)); it != sessions_.end()) {
    it->second.publishedNamespaces.insert(std::move(trackNamespace));
  }
}

void ClientNetworkMetricsStore::removePublishedNamespace(
    std::string_view connectionId,
    std::string_view trackNamespace) {
  std::lock_guard lock(mutex_);
  if (auto it = sessions_.find(std::string(connectionId)); it != sessions_.end()) {
    it->second.publishedNamespaces.erase(std::string(trackNamespace));
  }
}

void ClientNetworkMetricsStore::addTrackSubscription(
    std::string_view connectionId,
    std::string track) {
  std::lock_guard lock(mutex_);
  if (auto it = sessions_.find(std::string(connectionId)); it != sessions_.end()) {
    it->second.trackSubscriptions.insert(std::move(track));
  }
}

void ClientNetworkMetricsStore::removeTrackSubscription(
    std::string_view connectionId,
    std::string_view track) {
  std::lock_guard lock(mutex_);
  if (auto it = sessions_.find(std::string(connectionId)); it != sessions_.end()) {
    it->second.trackSubscriptions.erase(std::string(track));
  }
}

void ClientNetworkMetricsStore::addNamespaceSubscription(
    std::string_view connectionId,
    std::string trackNamespace) {
  std::lock_guard lock(mutex_);
  if (auto it = sessions_.find(std::string(connectionId)); it != sessions_.end()) {
    it->second.namespaceSubscriptions.insert(std::move(trackNamespace));
  }
}

void ClientNetworkMetricsStore::removeNamespaceSubscription(
    std::string_view connectionId,
    std::string_view trackNamespace) {
  std::lock_guard lock(mutex_);
  if (auto it = sessions_.find(std::string(connectionId)); it != sessions_.end()) {
    it->second.namespaceSubscriptions.erase(std::string(trackNamespace));
  }
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
    currentPacketsAcked_ += ack.ackedPackets.size();
    metrics_.ect0 = std::max(metrics_.ect0, ack.ecnECT0Count);
    metrics_.ect1 = std::max(metrics_.ect1, ack.ecnECT1Count);
    metrics_.ce = std::max(metrics_.ce, ack.ecnCECount);
  }
  metrics_.ackedPackets = currentPacketsAcked_;
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
