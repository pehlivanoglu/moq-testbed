/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/Service.h"
#include "sbd/ReceiveIntervals.h"
#include <folly/json.h>
#include <folly/logging/xlog.h>
#include <filesystem>
#include <stdexcept>

namespace openmoq::moqx::sbd {
Service::Service(Config config, std::string relayId)
    : config_(std::move(config)), relayId_(std::move(relayId)) {
  if (config_.enabled && !config_.outputFile.empty()) {
    const auto parent = std::filesystem::path(config_.outputFile).parent_path();
    if (!parent.empty()) std::filesystem::create_directories(parent);
    output_.open(config_.outputFile, std::ios::app);
    if (!output_) throw std::runtime_error("Cannot open SBD output: " + config_.outputFile);
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
  if (!value) { flow->status = "not_client"; flow->summary = {}; }
}
void Service::publish(const std::string& id, const Summary& summary, std::string status) {
  std::lock_guard lock(mutex_);
  if (auto it = flows_.find(id); it != flows_.end()) {
    auto& flow = *it->second;
    if (flow.status != status)
      XLOG(INFO) << "SBD connection=" << id << " status=" << status;
    flow.summary = summary;
    flow.status = std::move(status);
    flow.updated = std::chrono::steady_clock::now();
  }
}
void Service::close(const std::string& id) {
  std::lock_guard lock(mutex_);
  if (auto it = flows_.find(id); it != flows_.end()) {
    it->second->eligible = false;
    it->second->status = "closed";
    it->second->updated = std::chrono::steady_clock::now();
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
  const auto wall = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::system_clock::now().time_since_epoch()).count();
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
    folly::dynamic members = folly::dynamic::array;
    for (const auto& g : groups)
      if (std::find(g.begin(), g.end(), id) != g.end())
        for (const auto& member : g) members.push_back(member);
    clients.push_back(folly::dynamic::object
      ("connection_id", id)("peer", f->peer)("status", status)
      ("eligible", bool(f->eligible))("video_started", bool(f->videoStarted))("intervals", m.intervals)
      ("full_history", m.intervals >= 2 * kN)("samples", m.samples)
      ("skew_est", m.skew)("var_est_us", m.variation)
      ("grouping_var_est_us", m.groupingVariation)("freq_est", m.frequency)
      ("pkt_loss", m.loss)("acked_window", m.acked)("lost_window", m.lost)
      ("bottleneck", m.bottleneck)("group", std::move(members)));
  }
  folly::dynamic grouped = folly::dynamic::array;
  for (const auto& g : groups) {
    folly::dynamic row = folly::dynamic::array;
    for (const auto& id : g) row.push_back(id);
    grouped.push_back(std::move(row));
  }
  latest_ = folly::toJson(folly::dynamic::object
    ("schema_version", 1)("algorithm", "lcn2014-pdv2-window-v4")("timestamp_ms", wall)("relay_id", relayId_)
    ("enabled", config_.enabled)("delay_source", config_.delaySource)
    ("c_s", 0.0)("p_f", kFrequencyThreshold)("p_d", kLossDifferenceThreshold)
    ("p_s", kSkewThreshold)("p_pdv", kVariationThreshold)("p_v", kCrossingThreshold)
    ("variability_estimator", "pdv2")("variance_threshold_mode", "relative_to_larger")
    ("loss_threshold_mode", "absolute_difference")("interval_clock", config_.delaySource == "owd" ? "receiver_timestamp" : "ack_arrival")
    ("loss_interval_clock", "packet_send")
    ("feedback_grace_ms", config_.delaySource == "owd" ? kFeedbackGraceUs / 1000 : 0)
    ("feedback_timeout_ms", config_.delaySource == "owd" ? kFeedbackTimeout.count() : kInterval.count())
    ("gap_policy", "reset_and_rewarm")
    ("interval_ms", kInterval.count())("N", kN)("M", kM)("p_l", kLossThreshold)
    ("clients", std::move(clients))("groups", std::move(grouped)));
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
