/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/WeiDetector.h"

#include <folly/json.h>
#include <folly/logging/xlog.h>

#include <algorithm>
#include <filesystem>
#include <limits>
#include <stdexcept>

namespace openmoq::moqx::sbd {
namespace {

folly::dynamic jsonStrings(const std::set<std::string>& values) {
  folly::dynamic result = folly::dynamic::array;
  for (const auto& value : values) {
    result.push_back(value);
  }
  return result;
}

uint64_t saturatedAdd(uint64_t left, uint64_t right) {
  return right > std::numeric_limits<uint64_t>::max() - left
      ? std::numeric_limits<uint64_t>::max()
      : left + right;
}

} // namespace

WeiDetector::WeiDetector(bool enabled, std::string outputFile, std::string relayId)
    : enabled_(enabled),
      outputFile_(std::move(outputFile)),
      relayId_(std::move(relayId)) {
  if (enabled_ && !outputFile_.empty()) {
    const auto parent = std::filesystem::path(outputFile_).parent_path();
    if (!parent.empty()) {
      std::filesystem::create_directories(parent);
    }
    output_.open(outputFile_, std::ios::app);
    if (!output_) {
      throw std::runtime_error("Cannot open SBD output: " + outputFile_);
    }
    events_.open(outputFile_ + ".events.jsonl", std::ios::app);
    if (!events_) {
      throw std::runtime_error("Cannot open SBD event output: " + outputFile_ + ".events.jsonl");
    }
  }
  snapshotLocked(false);
}

int64_t WeiDetector::monotonicUs(TimePoint time) {
  return std::chrono::duration_cast<std::chrono::microseconds>(time.time_since_epoch()).count();
}

int64_t WeiDetector::unixNs() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

const char* WeiDetector::stateName(State state) {
  switch (state) {
    case State::Monitoring:
      return "MONITORING";
    case State::Judgement:
      return "JUDGEMENT";
    case State::FinalJudgement:
      return "FINAL_JUDGEMENT";
  }
  return "MONITORING";
}

const char* WeiDetector::stageName(Stage stage) {
  switch (stage) {
    case Stage::Preliminary:
      return "preliminary";
    case Stage::VerifyPreliminary:
      return "verify_preliminary";
    case Stage::VerifyFinal:
      return "verify_final";
  }
  return "preliminary";
}

const char* WeiDetector::eventName(EventType type) {
  return type == EventType::Ce ? "CE" : "loss";
}

WeiDetector::Connection& WeiDetector::connection(const std::string& id) {
  return connections_[id];
}

uint64_t WeiDetector::halfWindow(const Connection& connection) {
  if (!connection.maxDatagramSize) {
    return 0;
  }
  return (connection.cwndBytes / connection.maxDatagramSize) / 2;
}

void WeiDetector::attach(const std::string& id, const std::string& peer) {
  if (!enabled_ || id.empty()) {
    return;
  }
  std::lock_guard lock(mutex_);
  auto& flow = connection(id);
  flow.peer = peer;
  flow.closed = false;
  snapshotLocked(false);
}

void WeiDetector::eligible(const std::string& id, bool value) {
  if (!enabled_ || id.empty()) {
    return;
  }
  std::lock_guard lock(mutex_);
  auto& flow = connection(id);
  if (flow.eligible == value && !flow.closed) {
    return;
  }
  if (!value) {
    removeFromDetection(id, "ineligible");
  } else {
    flow.eligible = true;
    flow.closed = false;
    flow.state = State::Monitoring;
    flow.groupId.reset();
    flow.events.clear();
    flow.packetIndex.clear();
    flow.resolvedPackets.clear();
    flow.sentPackets = 0;
    flow.feedbackWatermark = 0;
  }
  snapshotLocked(true);
}

void WeiDetector::close(const std::string& id) {
  if (!enabled_ || id.empty()) {
    return;
  }
  std::lock_guard lock(mutex_);
  removeFromDetection(id, "connection_closed");
  if (auto it = connections_.find(id); it != connections_.end()) {
    it->second.closed = true;
  }
  snapshotLocked(true);
}

void WeiDetector::packetsWritten(
    const std::string& id,
    const std::vector<uint64_t>& packetNumbers,
    uint64_t cwndBytes,
    uint64_t maxDatagramSize) {
  if (!enabled_ || packetNumbers.empty()) {
    return;
  }
  std::lock_guard lock(mutex_);
  auto& flow = connection(id);
  flow.cwndBytes = cwndBytes;
  flow.maxDatagramSize = maxDatagramSize;
  for (const auto packetNumber : packetNumbers) {
    if (flow.packetIndex.contains(packetNumber)) {
      continue;
    }
    flow.packetIndex.emplace(packetNumber, ++flow.sentPackets);
  }
}

void WeiDetector::resolvePackets(
    Connection& flow,
    const std::vector<uint64_t>& packetNumbers) {
  for (const auto packetNumber : packetNumbers) {
    auto it = flow.packetIndex.find(packetNumber);
    if (it == flow.packetIndex.end()) {
      continue;
    }
    flow.resolvedPackets.insert(it->second);
    flow.packetIndex.erase(it);
  }
  while (flow.resolvedPackets.erase(flow.feedbackWatermark + 1)) {
    ++flow.feedbackWatermark;
  }
}

void WeiDetector::recordEvent(
    const std::string&,
    Connection& flow,
    EventType type,
    TimePoint time,
    uint64_t count) {
  flow.events.push_back(CongestionEvent{type, time, flow.sentPackets, count});
}

void WeiDetector::ackEcn(
    const std::string& id,
    TimePoint ackTime,
    const std::vector<uint64_t>& ackedPacketNumbers,
    uint64_t ect0,
    uint64_t ect1,
    uint64_t ce,
    uint64_t cwndBytes,
    uint64_t maxDatagramSize) {
  if (!enabled_) {
    return;
  }
  std::lock_guard lock(mutex_);
  auto& flow = connection(id);
  flow.cwndBytes = cwndBytes;
  flow.maxDatagramSize = maxDatagramSize;
  resolvePackets(flow, ackedPacketNumbers);

  flow.ect0 = std::max(flow.ect0, ect0);
  flow.ect1 = std::max(flow.ect1, ect1);
  const uint64_t previousCe = flow.ce;
  flow.ce = std::max(flow.ce, ce);
  const uint64_t deltaCe = flow.ce - previousCe;
  if (deltaCe && flow.eligible && !flow.closed) {
    const auto eventUnixNs = unixNs();
    recordEvent(id, flow, EventType::Ce, ackTime, deltaCe);
    logCongestionEvent(id, flow, EventType::Ce, ackTime, deltaCe, deltaCe);
    startObservation(id, flow, ackTime, eventUnixNs, deltaCe);
  }
  const bool decided = completeReadyObservations();
  if (deltaCe || decided) {
    snapshotLocked(true);
  }
}

void WeiDetector::loss(
    const std::string& id,
    TimePoint lossTime,
    const std::vector<uint64_t>& lostPacketNumbers,
    uint64_t cwndBytes,
    uint64_t maxDatagramSize) {
  if (!enabled_ || lostPacketNumbers.empty()) {
    return;
  }
  std::lock_guard lock(mutex_);
  auto& flow = connection(id);
  flow.cwndBytes = cwndBytes;
  flow.maxDatagramSize = maxDatagramSize;
  resolvePackets(flow, lostPacketNumbers);
  if (flow.eligible && !flow.closed) {
    recordEvent(id, flow, EventType::Loss, lossTime, lostPacketNumbers.size());
    logCongestionEvent(
        id, flow, EventType::Loss, lossTime, lostPacketNumbers.size());
  }
  completeReadyObservations();
  snapshotLocked(true);
}

bool WeiDetector::hasEvidence(const Connection& flow, const Window& window) const {
  return std::any_of(flow.events.begin(), flow.events.end(), [&](const auto& event) {
    return event.packetIndex >= window.lower && event.packetIndex <= window.upper;
  });
}

bool WeiDetector::observationReady(const Observation& observation) const {
  for (const auto& [id, window] : observation.windows) {
    const auto it = connections_.find(id);
    if (it == connections_.end() || !it->second.eligible || it->second.closed) {
      continue;
    }
    if (it->second.feedbackWatermark < window.upper) {
      return false;
    }
  }
  return true;
}

void WeiDetector::startObservation(
    const std::string& trigger,
    Connection& flow,
    TimePoint triggerTime,
    int64_t triggerUnixNs,
    uint64_t ceDelta) {
  Stage stage;
  std::optional<uint64_t> groupId;
  if (flow.state == State::Monitoring) {
    stage = Stage::Preliminary;
    const bool alreadyPending = std::any_of(
        observations_.begin(), observations_.end(), [&](const auto& item) {
          return item.second.stage == Stage::Preliminary && item.second.trigger == trigger;
        });
    if (alreadyPending) {
      return;
    }
  } else {
    if (!flow.groupId || !groups_.contains(*flow.groupId)) {
      return;
    }
    groupId = flow.groupId;
    const bool alreadyPending = std::any_of(
        observations_.begin(), observations_.end(), [&](const auto& item) {
          return item.second.groupId == groupId;
        });
    if (alreadyPending) {
      return;
    }
    stage = flow.state == State::Judgement
        ? Stage::VerifyPreliminary
        : Stage::VerifyFinal;
  }

  Observation observation;
  observation.id = nextObservationId_++;
  observation.stage = stage;
  observation.trigger = trigger;
  observation.triggerTime = triggerTime;
  observation.triggerUnixNs = triggerUnixNs;
  observation.triggerCwndBytes = flow.cwndBytes;
  observation.triggerMaxDatagramSize = flow.maxDatagramSize;
  observation.triggerHalf = halfWindow(flow);
  observation.ceDelta = ceDelta;
  observation.groupId = groupId;

  for (const auto& [id, candidate] : connections_) {
    if (id == trigger || !candidate.eligible || candidate.closed) {
      continue;
    }
    bool include = candidate.state == State::Monitoring;
    if (groupId) {
      const auto group = groups_.find(*groupId);
      include = include || (group != groups_.end() && group->second.members.contains(id));
    }
    if (!include) {
      continue;
    }
    const uint64_t half = halfWindow(candidate);
    observation.windows.emplace(
        id,
        Window{
            candidate.sentPackets,
            half,
            candidate.sentPackets > half ? candidate.sentPackets - half : 0,
            saturatedAdd(candidate.sentPackets, half)});
  }
  const auto observationId = observation.id;
  observations_.emplace(observationId, std::move(observation));
  logObservation(observations_.at(observationId), "judgement_started");
}

bool WeiDetector::completeReadyObservations() {
  bool completed = false;
  while (true) {
    auto ready = std::find_if(observations_.begin(), observations_.end(), [&](const auto& item) {
      return observationReady(item.second);
    });
    if (ready == observations_.end()) {
      break;
    }
    auto observation = std::move(ready->second);
    observations_.erase(ready);
    completeObservation(std::move(observation));
    completed = true;
  }
  return completed;
}

void WeiDetector::completeObservation(Observation observation) {
  auto triggerIt = connections_.find(observation.trigger);
  if (triggerIt == connections_.end() || !triggerIt->second.eligible ||
      triggerIt->second.closed) {
    logDecision(observation, {}, false, "trigger_inactive", std::nullopt);
    return;
  }

  if (observation.stage == Stage::Preliminary) {
    if (triggerIt->second.state != State::Monitoring) {
      logDecision(observation, {}, false, "trigger_no_longer_monitoring", std::nullopt);
      return;
    }
    std::set<std::string> members{observation.trigger};
    for (const auto& [id, window] : observation.windows) {
      auto it = connections_.find(id);
      if (it != connections_.end() && it->second.eligible && !it->second.closed &&
          it->second.state == State::Monitoring && hasEvidence(it->second, window)) {
        members.insert(id);
      }
    }
    if (members.size() < 2) {
      logDecision(observation, members, false, "no_preliminary_set", std::nullopt);
      return;
    }
    Group group;
    group.id = nextGroupId_++;
    group.members = members;
    group.created = Clock::now();
    group.createdUnixNs = unixNs();
    const auto groupId = group.id;
    groups_.emplace(groupId, std::move(group));
    for (const auto& id : members) {
      auto& member = connections_.at(id);
      member.state = State::Judgement;
      member.groupId = groupId;
    }
    logDecision(observation, members, true, "preliminary_set_created", groupId);
    return;
  }

  if (!observation.groupId || !groups_.contains(*observation.groupId)) {
    logDecision(observation, {}, false, "group_no_longer_exists", observation.groupId);
    return;
  }
  auto& group = groups_.at(*observation.groupId);
  const State expected = observation.stage == Stage::VerifyPreliminary
      ? State::Judgement
      : State::FinalJudgement;
  if (triggerIt->second.groupId != observation.groupId ||
      triggerIt->second.state != expected) {
    logDecision(
        observation, group.members, false, "group_state_changed", observation.groupId);
    return;
  }

  bool membersMatch = true;
  bool outsiderMatch = false;
  for (const auto& memberId : group.members) {
    if (memberId == observation.trigger) {
      continue;
    }
    const auto window = observation.windows.find(memberId);
    const auto member = connections_.find(memberId);
    if (window == observation.windows.end() || member == connections_.end() ||
        !member->second.eligible || member->second.closed ||
        !hasEvidence(member->second, window->second)) {
      membersMatch = false;
      break;
    }
  }
  for (const auto& [id, window] : observation.windows) {
    if (group.members.contains(id)) {
      continue;
    }
    const auto it = connections_.find(id);
    if (it != connections_.end() && it->second.eligible && !it->second.closed &&
        it->second.state == State::Monitoring && hasEvidence(it->second, window)) {
      outsiderMatch = true;
      break;
    }
  }

  if (membersMatch && !outsiderMatch) {
    const bool wasConfirmed = group.confirmed;
    if (!group.confirmed) {
      group.confirmed = true;
      group.confirmedAt = Clock::now();
      group.confirmedUnixNs = unixNs();
      for (const auto& id : group.members) {
        connections_.at(id).state = State::FinalJudgement;
      }
    }
    logDecision(
        observation,
        group.members,
        true,
        wasConfirmed ? "confirmed_set_verified" : "preliminary_set_confirmed",
        group.id);
    return;
  }

  const auto members = group.members;
  const auto groupId = group.id;
  logDecision(
      observation,
      members,
      false,
      outsiderMatch ? "outside_connection_matched" : "member_missing_evidence",
      groupId);
  dissolveGroup(groupId, "verification_failed");
}

void WeiDetector::dissolveGroup(uint64_t groupId, std::string_view reason) {
  auto it = groups_.find(groupId);
  if (it == groups_.end()) {
    return;
  }
  const auto group = it->second;
  for (const auto& id : group.members) {
    auto member = connections_.find(id);
    if (member != connections_.end()) {
      member->second.state = State::Monitoring;
      member->second.groupId.reset();
    }
  }
  std::erase_if(observations_, [&](const auto& item) {
    return item.second.groupId == groupId;
  });
  if (events_.is_open()) {
    events_ << folly::toJson(folly::dynamic::object
      ("schema_version", 1)("algorithm", kWei2020ECN)("event", "group_invalidated")
      ("relay_id", relayId_)("group_id", groupId)("confirmed", group.confirmed)
      ("members", jsonStrings(group.members))("reason", reason)
      ("group_created_mono_us", monotonicUs(group.created))
      ("group_created_unix_ns", group.createdUnixNs)
      ("group_confirmed_mono_us", group.confirmed ? monotonicUs(group.confirmedAt) : 0)
      ("group_confirmed_unix_ns", group.confirmedUnixNs)
      ("invalidated_mono_us", monotonicUs(Clock::now()))
      ("invalidated_unix_ns", unixNs())) << '\n';
    events_.flush();
  }
  groups_.erase(it);
}

void WeiDetector::removeFromDetection(const std::string& id, std::string_view reason) {
  auto it = connections_.find(id);
  if (it == connections_.end()) {
    return;
  }
  if (it->second.groupId) {
    dissolveGroup(*it->second.groupId, reason);
  }
  it->second.eligible = false;
  it->second.state = State::Monitoring;
  it->second.groupId.reset();
  it->second.events.clear();
  std::erase_if(observations_, [&](auto& item) {
    if (item.second.trigger == id) {
      return true;
    }
    item.second.windows.erase(id);
    return false;
  });
  completeReadyObservations();
}

void WeiDetector::logCongestionEvent(
    const std::string& id,
    const Connection& flow,
    EventType type,
    TimePoint time,
    uint64_t count,
    uint64_t ceDelta) {
  if (!events_.is_open()) {
    return;
  }
  events_ << folly::toJson(folly::dynamic::object
    ("schema_version", 1)("algorithm", kWei2020ECN)("event", "congestion_event")
    ("relay_id", relayId_)("event_mono_us", monotonicUs(time))
    ("event_unix_ns", unixNs())("triggering_connection", id)
    ("event_type", eventName(type))("event_count", count)("ce_delta", ceDelta)
    ("latest_ect0", flow.ect0)("latest_ect1", flow.ect1)("latest_ce", flow.ce)
    ("packet_index", flow.sentPackets)("cwnd_bytes", flow.cwndBytes)
    ("max_datagram_size", flow.maxDatagramSize)
    ("half_window_packets", halfWindow(flow))("state", stateName(flow.state))) << '\n';
  events_.flush();
}

void WeiDetector::logObservation(
    const Observation& observation,
    std::string_view event) {
  if (!events_.is_open()) {
    return;
  }
  folly::dynamic windows = folly::dynamic::array;
  for (const auto& [id, window] : observation.windows) {
    windows.push_back(folly::dynamic::object
      ("connection_id", id)("anchor_packet", window.anchor)
      ("half_window_packets", window.half)("lower_packet", window.lower)
      ("upper_packet", window.upper));
  }
  folly::dynamic row = folly::dynamic::object
    ("schema_version", 1)("algorithm", kWei2020ECN)("event", event)
    ("relay_id", relayId_)("observation_id", observation.id)
    ("stage", stageName(observation.stage))
    ("triggering_connection", observation.trigger)
    ("trigger_event_type", "CE")("ce_delta", observation.ceDelta)
    ("trigger_mono_us", monotonicUs(observation.triggerTime))
    ("trigger_unix_ns", observation.triggerUnixNs)
    ("trigger_cwnd_bytes", observation.triggerCwndBytes)
    ("trigger_max_datagram_size", observation.triggerMaxDatagramSize)
    ("trigger_half_window_packets", observation.triggerHalf)
    ("windows", std::move(windows));
  row["group_id"] = observation.groupId
      ? folly::dynamic(*observation.groupId)
      : folly::dynamic(nullptr);
  events_ << folly::toJson(row) << '\n';
  events_.flush();
}

void WeiDetector::logDecision(
    const Observation& observation,
    const std::set<std::string>& members,
    bool success,
    std::string_view result,
    std::optional<uint64_t> groupId) {
  if (!events_.is_open()) {
    return;
  }
  folly::dynamic evidence = folly::dynamic::array;
  for (const auto& [id, window] : observation.windows) {
    const auto it = connections_.find(id);
    evidence.push_back(folly::dynamic::object
      ("connection_id", id)("half_window_packets", window.half)
      ("lower_packet", window.lower)("upper_packet", window.upper)
      ("congestion_evidence", it != connections_.end() && hasEvidence(it->second, window)));
  }
  folly::dynamic row = folly::dynamic::object
    ("schema_version", 1)("algorithm", kWei2020ECN)("event", "judgement_completed")
    ("relay_id", relayId_)("observation_id", observation.id)
    ("stage", stageName(observation.stage))
    ("triggering_connection", observation.trigger)
    ("trigger_event_type", "CE")("trigger_mono_us", monotonicUs(observation.triggerTime))
    ("decision_mono_us", monotonicUs(Clock::now()))("decision_unix_ns", unixNs())
    ("verification_success", success)("verification_result", result)
    ("candidate_members", jsonStrings(members))("evidence", std::move(evidence));
  row["group_id"] = groupId ? folly::dynamic(*groupId) : folly::dynamic(nullptr);
  if (groupId) {
    if (const auto group = groups_.find(*groupId); group != groups_.end()) {
      row["group_created_mono_us"] = monotonicUs(group->second.created);
      row["group_created_unix_ns"] = group->second.createdUnixNs;
      row["group_confirmed_mono_us"] = group->second.confirmed
          ? monotonicUs(group->second.confirmedAt)
          : 0;
      row["group_confirmed_unix_ns"] = group->second.confirmedUnixNs;
    }
  }
  events_ << folly::toJson(row) << '\n';
  events_.flush();
}

std::string WeiDetector::buildSnapshotLocked(int64_t wallNs) const {
  folly::dynamic clients = folly::dynamic::array;
  folly::dynamic independent = folly::dynamic::array;
  for (const auto& [id, flow] : connections_) {
    folly::dynamic row = folly::dynamic::object
      ("connection_id", id)("peer", flow.peer)("eligible", flow.eligible)
      ("active", !flow.closed)("state", stateName(flow.state))
      ("latest_ect0", flow.ect0)("latest_ect1", flow.ect1)("latest_ce", flow.ce)
      ("cwnd_bytes", flow.cwndBytes)("max_datagram_size", flow.maxDatagramSize)
      ("cwnd_packets", flow.maxDatagramSize ? flow.cwndBytes / flow.maxDatagramSize : 0)
      ("half_window_packets", halfWindow(flow))("sent_packets", flow.sentPackets)
      ("feedback_watermark", flow.feedbackWatermark);
    row["group_id"] = flow.groupId ? folly::dynamic(*flow.groupId) : folly::dynamic(nullptr);
    clients.push_back(std::move(row));
    if (flow.eligible && !flow.closed && flow.state == State::Monitoring) {
      independent.push_back(id);
    }
  }

  folly::dynamic groups = folly::dynamic::array;
  folly::dynamic groupRecords = folly::dynamic::array;
  folly::dynamic candidateGroups = folly::dynamic::array;
  for (const auto& [id, group] : groups_) {
    if (group.confirmed) {
      groups.push_back(jsonStrings(group.members));
    } else {
      candidateGroups.push_back(jsonStrings(group.members));
    }
    groupRecords.push_back(folly::dynamic::object
      ("group_id", id)("state", group.confirmed ? "FINAL_JUDGEMENT" : "JUDGEMENT")
      ("members", jsonStrings(group.members))
      ("created_mono_us", monotonicUs(group.created))
      ("created_unix_ns", group.createdUnixNs)
      ("confirmed_mono_us", group.confirmed ? monotonicUs(group.confirmedAt) : 0)
      ("confirmed_unix_ns", group.confirmedUnixNs));
  }

  return folly::toJson(folly::dynamic::object
    ("schema_version", 1)("algorithm", kWei2020ECN)
    ("timestamp_ms", wallNs / 1'000'000)("snapshot_unix_ns", wallNs)
    ("relay_id", relayId_)("enabled", enabled_)
    ("trigger_signal", "ACK_ECN_CE_COUNTER_INCREASE")
    ("evidence_signals", folly::dynamic::array("CE", "QUIC_DECLARED_LOSS"))
    ("window_basis", "ack_eliciting_app_data_packet_ordinal")
    ("future_window_completion", "acked_or_declared_lost_through_upper_packet")
    ("pto_is_congestion_event", false)
    ("clients", std::move(clients))("groups", std::move(groups))
    ("candidate_groups", std::move(candidateGroups))
    ("group_records", std::move(groupRecords))
    ("independent", std::move(independent)));
}

void WeiDetector::snapshotLocked(bool archive) {
  latest_ = buildSnapshotLocked(unixNs());

  if (!output_.is_open()) {
    return;
  }
  if (archive) {
    output_ << latest_ << '\n';
    output_.flush();
  }
  const auto latestPath = outputFile_ + ".latest.json";
  std::ofstream latestFile(latestPath + ".tmp");
  latestFile << latest_;
  latestFile.close();
  if (latestFile) {
    std::error_code error;
    std::filesystem::rename(latestPath + ".tmp", latestPath, error);
    if (error) {
      XLOG(ERR) << "SBD snapshot write failed: " << error.message();
    }
  }
}

std::string WeiDetector::json() const {
  std::lock_guard lock(mutex_);
  return buildSnapshotLocked(unixNs());
}

} // namespace openmoq::moqx::sbd
