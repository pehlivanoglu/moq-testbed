/* SPDX-License-Identifier: Apache-2.0 */
#pragma once
#include "sbd/Detector.h"
#include <atomic>
#include <condition_variable>
#include <fstream>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <unordered_map>

namespace openmoq::moqx::sbd {
struct Config {
  bool enabled{false};
  std::string delaySource{"owd"};
  std::string outputFile;
};
struct Flow {
  std::atomic<bool> eligible{false};
  std::atomic<bool> videoStarted{false};
  std::string peer;
  Summary summary;
  std::string status{"waiting_for_subscription"};
  std::chrono::steady_clock::time_point updated{};
  int64_t updatedUnixNs{0};
  std::optional<bool> lastBottleneck;
};
// Publishes only interval summaries; the packet path never takes this mutex.
class Service {
 public:
  explicit Service(Config config, std::string relayId);
  ~Service();
  const Config& config() const { return config_; }
  std::shared_ptr<Flow> attach(const std::string& id, const std::string& peer);
  void eligible(const std::string& id, bool value);
  void publish(const std::string& id, const Summary& summary, std::string status);
  void status(const std::string& id, std::string status);
  void close(const std::string& id);
  std::string json() const;
 private:
  void run();
  void snapshot();
  Config config_;
  std::string relayId_;
  mutable std::mutex mutex_;
  std::condition_variable wake_;
  bool stopping_{false};
  std::unordered_map<std::string, std::shared_ptr<Flow>> flows_;
  std::string latest_;
  std::ofstream output_, events_;
  std::vector<std::vector<std::string>> lastGroups_;
  std::thread worker_;
};
} // namespace openmoq::moqx::sbd
