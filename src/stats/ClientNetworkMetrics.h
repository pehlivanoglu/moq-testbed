/*
 * Copyright (c) OpenMOQ contributors.
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include <chrono>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include <quic/api/QuicSocket.h>
#include <quic/observer/SocketObserverTypes.h>

namespace openmoq::moqx::stats {

// One readily-consumable sample per downstream QUIC connection.  Times and
// byte counts describe the relay -> client path; RTT describes both paths.
struct ClientNetworkMetrics {
  std::string connectionId;
  std::string peer;
  bool active{true};
  bool appLimited{false};
  bool ecnCapable{false};

  uint64_t srttUs{0};
  uint64_t rttVarUs{0};
  uint64_t minRttUs{0};
  uint64_t queueDelayUs{0};
  uint64_t cwndBytes{0};
  uint64_t inflightBytes{0};
  uint64_t writableBytes{0};
  uint64_t ackedRateBps{0};
  uint64_t pacingRateBps{0};
  uint64_t ackedPackets{0};
  uint64_t lostPackets{0};
  uint64_t retransmittedPackets{0};
  uint64_t ect0{0};
  uint64_t ect1{0};
  uint64_t ce{0};
  double ceFraction{0.0};
  double l4sWeight{0.0};
  std::chrono::steady_clock::time_point updatedAt{};

  uint64_t windowSamples{0};
  uint64_t windowDurationMs{0};
  uint64_t windowAckedPackets{0};
  uint64_t windowEct0{0};
  uint64_t windowEct1{0};
  uint64_t windowCe{0};
  uint64_t windowLostPackets{0};
  uint64_t windowRetransmittedPackets{0};
  double windowCeFraction{0.0};
  double windowLossRate{0.0};
  int64_t windowQueueDelayTrendUs{0};
  int64_t windowSrttTrendUs{0};
  int64_t windowAckedRateTrendBps{0};
  int64_t windowCwndTrendBytes{0};
  double windowWritableBlockedFraction{0.0};
  double windowAppLimitedFraction{0.0};
  bool windowCounterReset{false};

  bool sessionMapped{false};
  std::vector<std::string> publishedTracks;
  std::vector<std::string> publishedNamespaces;
  std::vector<std::string> trackSubscriptions;
  std::vector<std::string> namespaceSubscriptions;
};

// Thread-safe boundary between QUIC worker callbacks, the admin endpoint, and
// a future bottleneck/action controller.
class ClientNetworkMetricsStore {
public:
  explicit ClientNetworkMetricsStore(
      std::chrono::milliseconds historyWindow = std::chrono::seconds(10),
      std::chrono::milliseconds sampleInterval = std::chrono::milliseconds(250),
      std::chrono::milliseconds inactiveRetention = std::chrono::seconds(10));

  void put(ClientNetworkMetrics metrics);
  std::vector<ClientNetworkMetrics> snapshot() const;

  void registerSession(std::string connectionId);
  void unregisterSession(std::string_view connectionId);
  void addPublishedTrack(std::string_view connectionId, std::string track);
  void removePublishedTrack(std::string_view connectionId, std::string_view track);
  void addPublishedNamespace(std::string_view connectionId, std::string trackNamespace);
  void removePublishedNamespace(
      std::string_view connectionId,
      std::string_view trackNamespace);
  void addTrackSubscription(std::string_view connectionId, std::string track);
  void removeTrackSubscription(std::string_view connectionId, std::string_view track);
  void addNamespaceSubscription(
      std::string_view connectionId,
      std::string trackNamespace);
  void removeNamespaceSubscription(
      std::string_view connectionId,
      std::string_view trackNamespace);

private:
  struct HistorySample {
    ClientNetworkMetrics metrics;
    uint64_t newAckedPackets{0};
    uint64_t newEct0{0};
    uint64_t newEct1{0};
    uint64_t newCe{0};
    uint64_t newLostPackets{0};
    uint64_t newRetransmittedPackets{0};
    bool counterReset{false};
  };

  struct SessionMetadata {
    std::set<std::string> publishedTracks;
    std::set<std::string> publishedNamespaces;
    std::set<std::string> trackSubscriptions;
    std::set<std::string> namespaceSubscriptions;
  };

  mutable std::mutex mutex_;
  std::unordered_map<std::string, ClientNetworkMetrics> clients_;
  std::unordered_map<std::string, SessionMetadata> sessions_;
  mutable std::unordered_map<std::string, std::deque<HistorySample>> histories_;
  std::chrono::milliseconds historyWindow_;
  std::chrono::milliseconds sampleInterval_;
  std::chrono::milliseconds inactiveRetention_;
};

// Runs on the connection's EventBase.  It gathers on ACK/RTT/loss events and
// publishes at most every 100 ms, keeping policy work out of mvfst's ACK path.
class ClientNetworkMetricsObserver final : public quic::ManagedObserver {
public:
  ClientNetworkMetricsObserver(
      quic::QuicSocket& socket,
      std::shared_ptr<ClientNetworkMetricsStore> store);

  void acksProcessed(
      quic::QuicSocketLite*,
      const AcksProcessedEvent& event) override;
  void packetLossDetected(
      quic::QuicSocketLite*,
      const LossEvent& event) override;
  void rttSampleGenerated(
      quic::QuicSocketLite*,
      const PacketRTT& event) override;
  void l4sWeightUpdated(
      quic::QuicSocketLite*,
      const L4sWeightUpdateEvent& event) noexcept override;
  void pacingRateUpdated(
      quic::QuicSocketLite*,
      const PacingRateUpdateEvent& event) noexcept override;
  void appRateLimited(quic::QuicSocketLite*, const AppLimitedEvent&) override;
  void startWritingFromAppLimited(
      quic::QuicSocketLite*,
      const AppLimitedEvent&) override;
  void closing(quic::QuicSocketLite*, const ClosingEvent&) noexcept override;

private:
  void readTransportInfo(quic::QuicSocketLite& socket);
  void publish(bool force = false);

  std::shared_ptr<ClientNetworkMetricsStore> store_;
  ClientNetworkMetrics metrics_;
  uint64_t currentBytesAcked_{0};
  uint64_t currentPacketsAcked_{0};
  uint64_t lastRateBytesAcked_{0};
  uint64_t lastRateEcn_{0};
  uint64_t lastRateCe_{0};
  std::chrono::steady_clock::time_point lastPublish_{};
};

} // namespace openmoq::moqx::stats
