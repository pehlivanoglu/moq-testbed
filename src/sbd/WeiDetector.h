/* SPDX-License-Identifier: Apache-2.0 */
#pragma once

#include <chrono>
#include <cstdint>
#include <deque>
#include <fstream>
#include <map>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace openmoq::moqx::sbd {

inline constexpr std::string_view kWei2020ECN{"Wei2020ECN"};

// Relay-wide, sender-side implementation of Wei et al. Algorithm 1. It only
// detects groups; it never changes congestion control or packet scheduling.
class WeiDetector {
 public:
  using Clock = std::chrono::steady_clock;
  using TimePoint = Clock::time_point;

  WeiDetector(bool enabled, std::string outputFile, std::string relayId);

  void attach(const std::string& id, const std::string& peer);
  void eligible(const std::string& id, bool value);
  void close(const std::string& id);

  void packetsWritten(
      const std::string& id,
      const std::vector<uint64_t>& packetNumbers,
      uint64_t cwndBytes,
      uint64_t maxDatagramSize);
  void ackEcn(
      const std::string& id,
      TimePoint ackTime,
      const std::vector<uint64_t>& ackedPacketNumbers,
      uint64_t ect0,
      uint64_t ect1,
      uint64_t ce,
      uint64_t cwndBytes,
      uint64_t maxDatagramSize);
  void loss(
      const std::string& id,
      TimePoint lossTime,
      const std::vector<uint64_t>& lostPacketNumbers,
      uint64_t cwndBytes,
      uint64_t maxDatagramSize);

  std::string json() const;

 private:
  enum class State { Monitoring, Judgement, FinalJudgement };
  enum class EventType { Ce, Loss };
  enum class Stage { Preliminary, VerifyPreliminary, VerifyFinal };

  struct CongestionEvent {
    EventType type;
    TimePoint time;
    uint64_t packetIndex{0};
    uint64_t count{0};
  };

  struct Connection {
    std::string peer;
    bool eligible{false};
    bool closed{false};
    State state{State::Monitoring};
    std::optional<uint64_t> groupId;
    uint64_t ect0{0}, ect1{0}, ce{0};
    uint64_t cwndBytes{0}, maxDatagramSize{0};
    uint64_t sentPackets{0}, feedbackWatermark{0};
    std::unordered_map<uint64_t, uint64_t> packetIndex;
    std::set<uint64_t> resolvedPackets;
    std::deque<CongestionEvent> events;
  };

  struct Window {
    uint64_t anchor{0}, half{0}, lower{0}, upper{0};
  };

  struct Observation {
    uint64_t id{0};
    Stage stage{Stage::Preliminary};
    std::string trigger;
    TimePoint triggerTime{};
    int64_t triggerUnixNs{0};
    uint64_t triggerCwndBytes{0}, triggerMaxDatagramSize{0}, triggerHalf{0};
    uint64_t ceDelta{0};
    std::optional<uint64_t> groupId;
    std::map<std::string, Window> windows;
  };

  struct Group {
    uint64_t id{0};
    bool confirmed{false};
    std::set<std::string> members;
    TimePoint created{};
    int64_t createdUnixNs{0};
    TimePoint confirmedAt{};
    int64_t confirmedUnixNs{0};
  };

  Connection& connection(const std::string& id);
  static uint64_t halfWindow(const Connection& connection);
  static const char* stateName(State state);
  static const char* stageName(Stage stage);
  static const char* eventName(EventType type);
  static int64_t monotonicUs(TimePoint time);
  static int64_t unixNs();

  void resolvePackets(Connection& connection, const std::vector<uint64_t>& packetNumbers);
  void recordEvent(
      const std::string& id,
      Connection& connection,
      EventType type,
      TimePoint time,
      uint64_t count);
  bool hasEvidence(const Connection& connection, const Window& window) const;
  bool observationReady(const Observation& observation) const;
  void startObservation(
      const std::string& trigger,
      Connection& connection,
      TimePoint triggerTime,
      int64_t triggerUnixNs,
      uint64_t ceDelta);
  bool completeReadyObservations();
  void completeObservation(Observation observation);
  void dissolveGroup(uint64_t groupId, std::string_view reason);
  void removeFromDetection(const std::string& id, std::string_view reason);

  void logCongestionEvent(
      const std::string& id,
      const Connection& connection,
      EventType type,
      TimePoint time,
      uint64_t count,
      uint64_t ceDelta = 0);
  void logObservation(const Observation& observation, std::string_view event);
  void logDecision(
      const Observation& observation,
      const std::set<std::string>& members,
      bool success,
      std::string_view result,
      std::optional<uint64_t> groupId);
  std::string buildSnapshotLocked(int64_t wallNs) const;
  void snapshotLocked(bool archive);

  bool enabled_{false};
  std::string outputFile_;
  std::string relayId_;
  mutable std::mutex mutex_;
  std::map<std::string, Connection> connections_;
  std::map<uint64_t, Group> groups_;
  std::map<uint64_t, Observation> observations_;
  uint64_t nextGroupId_{1};
  uint64_t nextObservationId_{1};
  std::string latest_;
  std::ofstream output_;
  std::ofstream events_;
};

} // namespace openmoq::moqx::sbd
