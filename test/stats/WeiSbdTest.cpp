/* SPDX-License-Identifier: Apache-2.0 */
#include "sbd/WeiDetector.h"

#include <folly/json.h>
#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>
#include <initializer_list>
#include <unistd.h>

using namespace openmoq::moqx::sbd;

namespace {

constexpr uint64_t kMss = 1200;
constexpr uint64_t kCwnd = 4 * kMss;

class Scenario {
 public:
  explicit Scenario(std::string outputFile = {})
      : detector(true, std::move(outputFile), "test-relay") {}

  WeiDetector detector;

  void add(std::string id) {
    detector.attach(id, id + "-peer");
    detector.eligible(id, true);
  }

  void send(const std::string& id, std::initializer_list<uint64_t> packets) {
    detector.packetsWritten(id, packets, kCwnd, kMss);
  }

  void ack(
      const std::string& id,
      std::initializer_list<uint64_t> packets,
      uint64_t ce = 0) {
    detector.ackEcn(
        id,
        time(),
        packets,
        100,
        0,
        ce,
        kCwnd,
        kMss);
  }

  void loss(const std::string& id, std::initializer_list<uint64_t> packets) {
    detector.loss(id, time(), packets, kCwnd, kMss);
  }

  folly::dynamic snapshot() const {
    return folly::parseJson(detector.json());
  }

  void prepareThree() {
    for (const auto* id : {"a", "b", "c"}) {
      add(id);
      send(id, {1, 2, 3, 4, 5});
      ack(id, {1, 2, 3, 4});
    }
  }

  void preliminaryAB() {
    ack("a", {}, 1);
    loss("b", {5});
    send("b", {6, 7});
    ack("b", {6, 7});
    ack("c", {5});
    send("c", {6, 7});
    ack("c", {6, 7});
  }

  void advanceToNine() {
    ack("a", {5});
    send("a", {6, 7, 8, 9});
    ack("a", {6, 7, 8, 9}, 1);
    send("b", {8, 9});
    ack("b", {8, 9});
    send("c", {8, 9});
    ack("c", {8, 9});
  }

  void verifyAB(bool outsiderEvidence) {
    ack("a", {}, 2);
    send("b", {10});
    loss("b", {10});
    send("b", {11});
    ack("b", {11});
    send("c", {10});
    if (outsiderEvidence) {
      loss("c", {10});
    } else {
      ack("c", {10});
    }
    send("c", {11});
    ack("c", {11});
  }

 private:
  WeiDetector::TimePoint time() {
    return WeiDetector::TimePoint{} + std::chrono::seconds(++tick_);
  }

  int tick_{0};
};

const folly::dynamic& client(const folly::dynamic& snapshot, std::string_view id) {
  for (const auto& item : snapshot["clients"]) {
    if (item["connection_id"].asString() == id) {
      return item;
    }
  }
  throw std::runtime_error("missing client");
}

} // namespace

TEST(WeiSbd, CeTriggersLossEvidenceAndSecondCeConfirms) {
  Scenario s;
  s.prepareThree();
  s.preliminaryAB();

  auto snapshot = s.snapshot();
  ASSERT_EQ(snapshot["candidate_groups"].size(), 1);
  EXPECT_EQ(snapshot["candidate_groups"][0][0].asString(), "a");
  EXPECT_EQ(snapshot["candidate_groups"][0][1].asString(), "b");
  EXPECT_EQ(client(snapshot, "a")["state"].asString(), "JUDGEMENT");
  EXPECT_EQ(client(snapshot, "c")["state"].asString(), "MONITORING");

  s.advanceToNine();
  s.verifyAB(false);
  snapshot = s.snapshot();
  ASSERT_EQ(snapshot["groups"].size(), 1);
  ASSERT_EQ(snapshot["groups"][0].size(), 2);
  EXPECT_EQ(snapshot["groups"][0][0].asString(), "a");
  EXPECT_EQ(snapshot["groups"][0][1].asString(), "b");
  EXPECT_EQ(client(snapshot, "a")["state"].asString(), "FINAL_JUDGEMENT");
  EXPECT_EQ(client(snapshot, "b")["state"].asString(), "FINAL_JUDGEMENT");
  ASSERT_EQ(snapshot["independent"].size(), 1);
  EXPECT_EQ(snapshot["independent"][0].asString(), "c");
  EXPECT_FALSE(snapshot["pto_is_congestion_event"].asBool());
}

TEST(WeiSbd, LossDoesNotTriggerDetection) {
  Scenario s;
  for (const auto* id : {"a", "b"}) {
    s.add(id);
    s.send(id, {1, 2, 3});
    s.ack(id, {1, 2});
  }
  s.loss("a", {3});
  s.loss("b", {3});
  const auto snapshot = s.snapshot();
  EXPECT_TRUE(snapshot["groups"].empty());
  EXPECT_TRUE(snapshot["candidate_groups"].empty());
  EXPECT_EQ(client(snapshot, "a")["state"].asString(), "MONITORING");
  EXPECT_EQ(client(snapshot, "b")["state"].asString(), "MONITORING");
}

TEST(WeiSbd, EligibilityStartsFreshPacketProgressWithoutReplayingCe) {
  Scenario s;
  s.send("a", {1, 2, 3});
  s.ack("a", {1, 2}, 4);
  s.add("a");
  s.send("a", {4});
  s.ack("a", {3, 4}, 4);

  const auto snapshot = s.snapshot();
  EXPECT_EQ(client(snapshot, "a")["sent_packets"].asInt(), 1);
  EXPECT_EQ(client(snapshot, "a")["feedback_watermark"].asInt(), 1);
  EXPECT_EQ(client(snapshot, "a")["latest_ce"].asInt(), 4);
  EXPECT_TRUE(snapshot["candidate_groups"].empty());
}

TEST(WeiSbd, PreliminarySetMayContainMoreThanTwoConnections) {
  Scenario s;
  s.prepareThree();
  s.ack("a", {}, 1);
  s.loss("b", {5});
  s.loss("c", {5});
  for (const auto* id : {"b", "c"}) {
    s.send(id, {6, 7});
    s.ack(id, {6, 7});
  }

  const auto snapshot = s.snapshot();
  ASSERT_EQ(snapshot["candidate_groups"].size(), 1);
  ASSERT_EQ(snapshot["candidate_groups"][0].size(), 3);
  EXPECT_EQ(snapshot["candidate_groups"][0][0].asString(), "a");
  EXPECT_EQ(snapshot["candidate_groups"][0][1].asString(), "b");
  EXPECT_EQ(snapshot["candidate_groups"][0][2].asString(), "c");
}

TEST(WeiSbd, MonitoringOutsiderEvidenceRejectsVerification) {
  Scenario s;
  s.prepareThree();
  s.preliminaryAB();
  s.advanceToNine();
  s.verifyAB(true);

  const auto snapshot = s.snapshot();
  EXPECT_TRUE(snapshot["groups"].empty());
  EXPECT_TRUE(snapshot["candidate_groups"].empty());
  for (const auto* id : {"a", "b", "c"}) {
    EXPECT_EQ(client(snapshot, id)["state"].asString(), "MONITORING");
  }
}

TEST(WeiSbd, ConfirmedGroupIsDissolvedWhenMemberStopsMatching) {
  Scenario s;
  s.prepareThree();
  s.preliminaryAB();
  s.advanceToNine();
  s.verifyAB(false);
  ASSERT_EQ(s.snapshot()["groups"].size(), 1);

  for (const auto* id : {"a", "b", "c"}) {
    s.send(id, {12, 13});
    s.ack(id, {12, 13}, id[0] == 'a' ? 2 : 0);
  }
  s.ack("a", {}, 3);
  for (const auto* id : {"b", "c"}) {
    s.send(id, {14, 15});
    s.ack(id, {14, 15});
  }

  const auto snapshot = s.snapshot();
  EXPECT_TRUE(snapshot["groups"].empty());
  EXPECT_EQ(client(snapshot, "a")["state"].asString(), "MONITORING");
  EXPECT_EQ(client(snapshot, "b")["state"].asString(), "MONITORING");
}

TEST(WeiSbd, WindowSizeUsesCwndPacketsNotElapsedTime) {
  Scenario s;
  for (const auto* id : {"a", "b"}) {
    s.add(id);
    s.send(id, {1, 2, 3, 4, 5});
    s.ack(id, {1, 2, 3, 4});
  }
  s.ack("a", {}, 1);
  // Time advances by whole seconds in this synthetic trace, but B's evidence
  // remains in A's packet-domain episode at packet position five.
  s.loss("b", {5});
  s.send("b", {6, 7});
  s.ack("b", {6, 7});
  const auto snapshot = s.snapshot();
  ASSERT_EQ(snapshot["candidate_groups"].size(), 1);
  EXPECT_EQ(client(snapshot, "b")["half_window_packets"].asInt(), 2);
}

TEST(WeiSbd, ArchivesEventsDecisionsAndGroupLifetime) {
  char path[] = "/tmp/moqx-wei-sbd-XXXXXX";
  const int fd = mkstemp(path);
  ASSERT_GE(fd, 0);
  close(fd);
  std::filesystem::remove(path);
  {
    Scenario s(path);
    s.prepareThree();
    s.preliminaryAB();
    s.advanceToNine();
    s.verifyAB(false);
    for (const auto* id : {"a", "b", "c"}) {
      s.send(id, {12, 13});
      s.ack(id, {12, 13}, id[0] == 'a' ? 2 : 0);
    }
    s.ack("a", {}, 3);
    for (const auto* id : {"b", "c"}) {
      s.send(id, {14, 15});
      s.ack(id, {14, 15});
    }
  }

  std::ifstream input(std::string(path) + ".events.jsonl");
  std::string line;
  bool sawCe = false, sawLoss = false, sawPreliminary = false;
  bool sawConfirmation = false, sawInvalidation = false;
  while (std::getline(input, line)) {
    const auto event = folly::parseJson(line);
    const auto type = event["event"].asString();
    if (type == "congestion_event") {
      sawCe = sawCe || event["event_type"] == "CE";
      sawLoss = sawLoss || event["event_type"] == "loss";
      EXPECT_GT(event["event_mono_us"].asInt(), 0);
      EXPECT_EQ(event["cwnd_bytes"].asInt(), kCwnd);
      EXPECT_EQ(event["half_window_packets"].asInt(), 2);
    } else if (type == "judgement_completed" &&
               event["verification_result"] == "preliminary_set_created") {
      sawPreliminary = true;
      EXPECT_EQ(event["candidate_members"].size(), 2);
      EXPECT_GT(event["group_created_unix_ns"].asInt(), 0);
    } else if (type == "judgement_completed" &&
               event["verification_result"] == "preliminary_set_confirmed") {
      sawConfirmation = true;
      EXPECT_TRUE(event["verification_success"].asBool());
      EXPECT_GT(event["group_confirmed_unix_ns"].asInt(), 0);
    } else if (type == "group_invalidated") {
      sawInvalidation = true;
      EXPECT_GT(event["group_created_unix_ns"].asInt(), 0);
      EXPECT_GT(event["group_confirmed_unix_ns"].asInt(), 0);
      EXPECT_GT(event["invalidated_unix_ns"].asInt(), 0);
    }
  }
  EXPECT_TRUE(sawCe);
  EXPECT_TRUE(sawLoss);
  EXPECT_TRUE(sawPreliminary);
  EXPECT_TRUE(sawConfirmation);
  EXPECT_TRUE(sawInvalidation);
  std::filesystem::remove(path);
  std::filesystem::remove(std::string(path) + ".events.jsonl");
  std::filesystem::remove(std::string(path) + ".latest.json");
}
