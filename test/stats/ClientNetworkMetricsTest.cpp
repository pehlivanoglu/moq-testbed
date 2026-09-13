/*
 * Copyright (c) OpenMOQ contributors.
 * SPDX-License-Identifier: Apache-2.0
 */

#include "stats/ClientNetworkMetrics.h"

#include <chrono>

#include <gtest/gtest.h>

namespace openmoq::moqx::stats {
namespace {

using namespace std::chrono_literals;

ClientNetworkMetrics sample(
    std::chrono::steady_clock::time_point at,
    uint64_t acked,
    uint64_t ect1,
    uint64_t ce,
    uint64_t lost,
    uint64_t retransmitted) {
  ClientNetworkMetrics metrics;
  metrics.connectionId = "client";
  metrics.updatedAt = at;
  metrics.ackedPackets = acked;
  metrics.ect1 = ect1;
  metrics.ce = ce;
  metrics.lostPackets = lost;
  metrics.retransmittedPackets = retransmitted;
  return metrics;
}

TEST(ClientNetworkMetricsStoreTest, ComputesBoundedWindowDeltas) {
  ClientNetworkMetricsStore store(2s, 200ms, 2s);
  const auto now = std::chrono::steady_clock::now();

  auto first = sample(now - 500ms, 100, 10, 0, 0, 0);
  first.queueDelayUs = 100;
  first.srttUs = 150;
  first.ackedRateBps = 1'000'000;
  first.cwndBytes = 20'000;
  first.writableBytes = 100;
  store.put(first);

  auto second = sample(now - 250ms, 160, 30, 5, 4, 3);
  second.queueDelayUs = 300;
  second.srttUs = 400;
  second.ackedRateBps = 900'000;
  second.cwndBytes = 15'000;
  second.writableBytes = 0;
  store.put(second);

  auto third = sample(now, 220, 50, 15, 7, 5);
  third.queueDelayUs = 600;
  third.srttUs = 700;
  third.ackedRateBps = 700'000;
  third.cwndBytes = 10'000;
  third.writableBytes = 0;
  third.appLimited = true;
  store.put(third);

  const auto result = store.snapshot().front();
  EXPECT_EQ(result.windowSamples, 3);
  EXPECT_EQ(result.windowDurationMs, 500);
  EXPECT_EQ(result.windowAckedPackets, 120);
  EXPECT_EQ(result.windowEct1, 40);
  EXPECT_EQ(result.windowCe, 15);
  EXPECT_EQ(result.windowLostPackets, 7);
  EXPECT_EQ(result.windowRetransmittedPackets, 5);
  EXPECT_DOUBLE_EQ(result.windowCeFraction, 15.0 / 55.0);
  EXPECT_DOUBLE_EQ(result.windowLossRate, 7.0 / 127.0);
  EXPECT_EQ(result.windowQueueDelayTrendUs, 500);
  EXPECT_EQ(result.windowSrttTrendUs, 550);
  EXPECT_EQ(result.windowAckedRateTrendBps, -300'000);
  EXPECT_EQ(result.windowCwndTrendBytes, -10'000);
  EXPECT_DOUBLE_EQ(result.windowWritableBlockedFraction, 2.0 / 3.0);
  EXPECT_DOUBLE_EQ(result.windowAppLimitedFraction, 1.0 / 3.0);
  EXPECT_FALSE(result.windowCounterReset);
}

TEST(ClientNetworkMetricsStoreTest, LimitsHistorySamplingRate) {
  ClientNetworkMetricsStore store(2s, 250ms, 2s);
  const auto now = std::chrono::steady_clock::now();

  store.put(sample(now - 300ms, 10, 10, 0, 0, 0));
  store.put(sample(now - 200ms, 20, 20, 0, 0, 0));
  store.put(sample(now, 40, 40, 0, 0, 0));

  const auto result = store.snapshot().front();
  EXPECT_EQ(result.windowSamples, 2);
  EXPECT_EQ(result.windowAckedPackets, 30);
  EXPECT_EQ(result.windowEct1, 30);
}

TEST(ClientNetworkMetricsStoreTest, DoesNotUnderflowAfterCounterReset) {
  ClientNetworkMetricsStore store(2s, 0ms, 2s);
  const auto now = std::chrono::steady_clock::now();

  store.put(sample(now - 100ms, 100, 100, 50, 20, 20));
  store.put(sample(now, 10, 10, 5, 2, 2));

  const auto result = store.snapshot().front();
  EXPECT_TRUE(result.windowCounterReset);
  EXPECT_EQ(result.windowAckedPackets, 0);
  EXPECT_EQ(result.windowEct1, 0);
  EXPECT_EQ(result.windowCe, 0);
  EXPECT_EQ(result.windowLostPackets, 0);
  EXPECT_EQ(result.windowRetransmittedPackets, 0);
}

TEST(ClientNetworkMetricsStoreTest, ExpiresOldAndInactiveHistory) {
  const auto now = std::chrono::steady_clock::now();
  ClientNetworkMetricsStore windowStore(500ms, 0ms, 10s);
  windowStore.put(sample(now - 1s, 10, 10, 0, 0, 0));
  windowStore.put(sample(now - 100ms, 20, 20, 0, 0, 0));
  auto result = windowStore.snapshot().front();
  EXPECT_EQ(result.windowSamples, 1);
  EXPECT_EQ(result.windowAckedPackets, 0);

  ClientNetworkMetricsStore inactiveStore(10s, 0ms, 500ms);
  auto inactive = sample(now - 1s, 10, 10, 0, 0, 0);
  inactive.active = false;
  inactiveStore.put(std::move(inactive));
  result = inactiveStore.snapshot().front();
  EXPECT_EQ(result.windowSamples, 0);
}

} // namespace
} // namespace openmoq::moqx::stats
