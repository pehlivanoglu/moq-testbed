/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/Service.h"
#include "sbd/ReceiveIntervals.h"
#include <folly/json.h>
#include <folly/logging/xlog.h>
#include <filesystem>
#include <stdexcept>

namespace openmoq::moqx::sbd {
namespace {
int64_t unixNs() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count();
}
int64_t monotonicUs(std::chrono::steady_clock::time_point time) {
  return std::chrono::duration_cast<std::chrono::microseconds>(
      time.time_since_epoch()).count();
}
folly::dynamic jsonGroups(const std::vector<std::vector<std::string>>& groups) {
  folly::dynamic result = folly::dynamic::array;
  for (const auto& group : groups) {
    folly::dynamic row = folly::dynamic::array;
    for (const auto& id : group) row.push_back(id);
    result.push_back(std::move(row));
  }
  return result;
}
}

Service::Service(Config config, std::string relayId)
    : config_(std::move(config)), relayId_(std::move(relayId)) {
  if (config_.enabled && !config_.outputFile.empty()) {
    const auto parent = std::filesystem::path(config_.outputFile).parent_path();
    if (!parent.empty()) std::filesystem::create_directories(parent);
    output_.open(config_.outputFile, std::ios::app);
    if (!output_) throw std::runtime_error("Cannot open SBD output: " + config_.outputFile);
    events_.open(config_.outputFile + ".events.jsonl", std::ios::app);
    if (!events_) throw std::runtime_error("Cannot open SBD event output: " + config_.outputFile + ".events.jsonl");
  }
  snapshot();
  if (config_.enabled) worker_ = std::thread([this] { run(); });
}
Service::~Service() {
  { std::lock_guard lock(mutex_); stopping_ = true; }
  wake_.notify_all();
  if (worker_.joinable()) worker_.join();
}
std::shared_ptr<Flow> Service::attach(const std::string& id, const std::string& peer) {
  std::lock_guard lock(mutex_);
  auto& flow = flows_[id];
  if (!flow) flow = std::make_shared<Flow>();
  flow->peer = peer;
  return flow;
}
void Service::eligible(const std::string& id, bool value) {
  if (!config_.enabled) return;
  std::lock_guard lock(mutex_);
  auto& flow = flows_[id];
  if (!flow) flow = std::make_shared<Flow>();
  flow->eligible = value;
  if (value && !flow->videoStarted) flow->status = "waiting_for_video";
  if (!value) {
    flow->status = "not_client";
    flow->summary = {};
    flow->lastBottleneck.reset();
  }
}
void Service::publish(const std::string& id, const Summary& summary, std::string status) {
  std::lock_guard lock(mutex_);
  if (auto it = flows_.find(id); it != flows_.end()) {
    auto& flow = *it->second;
    const auto published = std::chrono::steady_clock::now();
    const auto publishedUnixNs = unixNs();
    const bool decisionValid = status == "ready" && summary.ready() && summary.measurementValid;
    const bool detected = decisionValid && summary.bottleneck;
    const bool previousDetected = flow.lastBottleneck.value_or(false);
    if (decisionValid && previousDetected != detected && events_.is_open()) {
      events_ << folly::toJson(folly::dynamic::object
        ("schema_version", 1)("event", "bottleneck_state_changed")("relay_id", relayId_)
        ("connection_id", id)("previous_bottleneck", previousDetected)
        ("bottleneck", detected)("detected_unix_ns", publishedUnixNs)
        ("detected_mono_us", monotonicUs(published))
        ("interval_start_mono_us", summary.intervalStartUs)
        ("interval_end_mono_us", summary.intervalEndUs)
        ("evidence_window_start_mono_us", summary.intervalEndUs -
          int64_t(std::min<uint64_t>(summary.intervals, kN)) * kIntervalUs)
        ("interval_delay_mean_us", summary.intervalMeanDelayUs)
        ("skew_est", summary.skew)("var_est_us", summary.variation)
        ("freq_est", summary.frequency)("pkt_loss", summary.loss)) << '\n';
      events_.flush();
      if (!events_) XLOG(ERR) << "SBD event write failed: " << config_.outputFile << ".events.jsonl";
    }
    if (decisionValid) flow.lastBottleneck = detected;
    else if (!summary.intervals) flow.lastBottleneck.reset();
    if (flow.status != status)
      XLOG(INFO) << "SBD connection=" << id << " status=" << status;
    flow.summary = summary;
    flow.status = std::move(status);
    flow.updated = published;
    flow.updatedUnixNs = publishedUnixNs;
  }
}
void Service::status(const std::string& id, std::string status) {
  std::lock_guard lock(mutex_);
  if (auto it = flows_.find(id); it != flows_.end() && it->second->status != status) {
    XLOG(INFO) << "SBD connection=" << id << " status=" << status;
    it->second->status = std::move(status);
  }
}
void Service::close(const std::string& id) {
  std::lock_guard lock(mutex_);
  if (auto it = flows_.find(id); it != flows_.end()) {
    it->second->eligible = false;
    it->second->status = "closed";
    it->second->updated = std::chrono::steady_clock::now();
    it->second->updatedUnixNs = unixNs();
  }
}
std::string Service::json() const { std::lock_guard lock(mutex_); return latest_; }
void Service::run() {
  auto next = std::chrono::steady_clock::now() + kInterval;
  std::unique_lock lock(mutex_);
  while (!wake_.wait_until(lock, next, [this] { return stopping_; })) {
    lock.unlock();
    snapshot();
    lock.lock();
    next += kInterval;
    if (next < std::chrono::steady_clock::now()) next = std::chrono::steady_clock::now() + kInterval;
  }
  lock.unlock();
  snapshot();
}
void Service::snapshot() {
  std::lock_guard lock(mutex_);
  const auto now = std::chrono::steady_clock::now();
  const auto wallNs = unixNs();
  const auto wall = wallNs / 1000000;
  const auto decisionMonoUs = monotonicUs(now);
  std::vector<FlowSummary> ready;
  for (const auto& [id, f] : flows_)
    if (f->eligible && f->status == "ready" && now - f->updated < 3 * kInterval)
      ready.push_back({id, f->summary});
  const auto groups = group(std::move(ready));
  folly::dynamic clients = folly::dynamic::array;
  for (const auto& [id, f] : flows_) {
    const auto& m = f->summary;
    std::string status = f->status;
    if (status == "ready" && now - f->updated >= 3 * kInterval) status = "stale";
    const bool decisionValid = status == "ready" && m.ready() && m.measurementValid;
    folly::dynamic members = folly::dynamic::array;
    for (const auto& g : groups)
      if (std::find(g.begin(), g.end(), id) != g.end())
        for (const auto& member : g) members.push_back(member);
    clients.push_back(folly::dynamic::object
      ("connection_id", id)("peer", f->peer)("status", status)
      ("eligible", bool(f->eligible))("video_started", bool(f->videoStarted))("intervals", m.intervals)
      ("decision_valid", decisionValid)
      ("full_history", m.groupingReady())("grouping_ready", m.groupingReady())("samples", m.samples)
      ("interval_start_mono_us", m.intervalStartUs)("interval_end_mono_us", m.intervalEndUs)
      ("evidence_window_start_mono_us", m.intervalEndUs - int64_t(std::min<uint64_t>(m.intervals, kN)) * kIntervalUs)
      ("measurement_published_mono_us", monotonicUs(f->updated))
      ("measurement_published_unix_ns", f->updatedUnixNs)
      ("interval_delay_mean_us", m.intervalMeanDelayUs)
      ("interval_delay_min_us", m.intervalMinDelayUs)
      ("interval_delay_max_us", m.intervalMaxDelayUs)
      ("skew_est", m.skew)("var_est_us", m.variation)
      ("grouping_var_est_us", m.groupingVariation)("freq_est", m.frequency)
      ("pkt_loss", m.loss)("acked_window", m.acked)("lost_window", m.lost)
      ("bottleneck", m.bottleneck)
      ("group", std::move(members)));
  }
  if (groups != lastGroups_ && events_.is_open()) {
    events_ << folly::toJson(folly::dynamic::object
      ("schema_version", 1)("event", "groups_changed")("relay_id", relayId_)
      ("decision_unix_ns", wallNs)("decision_mono_us", decisionMonoUs)
      ("previous_groups", jsonGroups(lastGroups_))("groups", jsonGroups(groups))) << '\n';
    events_.flush();
    if (!events_) XLOG(ERR) << "SBD event write failed: " << config_.outputFile << ".events.jsonl";
  }
  lastGroups_ = groups;
  latest_ = folly::toJson(folly::dynamic::object
    ("schema_version", 1)("algorithm", "lcn2014-pdv2-rfc-fill-v8")
    ("timestamp_ms", wall)("snapshot_unix_ns", wallNs)("group_decision_mono_us", decisionMonoUs)
    ("group_decision_unix_ns", wallNs)("relay_id", relayId_)
    ("enabled", config_.enabled)("delay_source", config_.delaySource)
    ("c_s", 0.0)("p_f", kFrequencyThreshold)("p_d", kLossDifferenceThreshold)
    ("p_s", kSkewThreshold)("p_pdv", kVariationThreshold)("p_v", kCrossingThreshold)
    ("variability_estimator", "pdv2")("variance_threshold_mode", "relative_to_larger")
    ("loss_threshold_mode", "absolute_difference")
    ("delay_measurement", config_.delaySource == "owd" ? "absolute_owd" : "rtt")
    ("receive_timestamp_basis", config_.delaySource == "owd" ? "linux_clock_monotonic" : "not_applicable")
    ("interval_clock", config_.delaySource == "owd" ? "receiver_clock_monotonic" : "ack_arrival")
    ("loss_interval_clock", "packet_send")
    ("feedback_grace_ms", 0)
    ("interval_completion", config_.delaySource == "owd" ?
        "receive_timestamp_watermark" : "ack_arrival_timer")
    ("feedback_timeout_ms", config_.delaySource == "owd" ? kFeedbackTimeout.count() : kInterval.count())
    ("gap_policy", "preserve_history_mark_invalid")
    ("parameter_precedence", "LCN2014_then_RFC8382")
    ("transport_gap_policy", "stale_without_reset")
    ("loss_correction", "packet_number_ledger")
    ("interval_ms", kInterval.count())("N", kN)("M", kM)
    ("metric_window_intervals", kN)("grouping_warmup_intervals", kGroupingWarmupIntervals)
    ("p_l", kLossThreshold)
    ("clients", std::move(clients))("groups", jsonGroups(groups)));
  if (output_.is_open()) {
    output_ << latest_ << '\n';
    output_.flush();
    if (!output_) XLOG(ERR) << "SBD archive write failed: " << config_.outputFile;
    // Atomic latest snapshot for the testbed visualizer; archive is append-only.
    const auto latestPath = config_.outputFile + ".latest.json";
    std::ofstream latestFile(latestPath + ".tmp");
    latestFile << latest_;
    latestFile.close();
    if (latestFile) {
      std::error_code error;
      std::filesystem::rename(latestPath + ".tmp", latestPath, error);
      if (error) XLOG(ERR) << "SBD snapshot write failed: " << error.message();
    }
  }
  std::erase_if(flows_, [&](const auto& item) {
    return item.second->status == "closed" && now - item.second->updated > std::chrono::seconds(10);
  });
}
} // namespace openmoq::moqx::sbd
