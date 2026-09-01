/*
 * Copyright (c) OpenMOQ contributors.
 * This source code is licensed under the Apache 2.0 license found in the
 * LICENSE file in the root directory of this source tree.
 */

#include "admin/MetricsHandler.h"

#include <algorithm>
#include <chrono>
#include <folly/CancellationToken.h>
#include <folly/coro/Task.h>
#include <folly/coro/WithCancellation.h>
#include <folly/io/IOBuf.h>
#include <folly/io/IOBufQueue.h>
#include <folly/io/async/EventBaseManager.h>
#include <folly/logging/xlog.h>
#include <proxygen/httpserver/ResponseBuilder.h>
#include <proxygen/lib/http/HTTPMessage.h>

#include "admin/AdminServer.h"
#include "admin/JsonWriter.h"
#include "stats/ClientNetworkMetrics.h"
#include "stats/StatsRegistry.h"

namespace openmoq::moqx::admin {

void registerMetricsRoute(
    AdminServer& adminServer,
    std::shared_ptr<stats::StatsRegistry> registry
) {
  adminServer.addRoute(
      "GET",
      "/metrics",
      [registry](auto /*req*/, auto /*body*/, auto* downstream, folly::CancellationToken cancelToken) {
        auto* evb = folly::EventBaseManager::get()->getEventBase();

        folly::coro::co_withCancellation(
            cancelToken,
            folly::coro::co_withExecutor(
                evb,
                [](auto reg, auto* ds, auto token) -> folly::coro::Task<void> {
                  stats::StatsSnapshot snap;
                  try {
                    snap = co_await reg->aggregateAsync();
                  } catch (const std::exception& e) {
                    XLOG(ERR) << "MetricsHandler: aggregateAsync threw: " << e.what();
                    if (!token.isCancellationRequested()) {
                      proxygen::ResponseBuilder(ds)
                          .status(500, proxygen::HTTPMessage::getDefaultReason(500))
                          .body(folly::IOBuf::copyBuffer("internal error\n"))
                          .sendWithEOM();
                    }
                    co_return;
                  }
                  if (token.isCancellationRequested()) {
                    co_return;
                  }
                  auto body = stats::StatsSnapshot::formatPrometheus(snap);
                  proxygen::ResponseBuilder(ds)
                      .status(200, proxygen::HTTPMessage::getDefaultReason(200))
                      .header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                      .body(std::move(body))
                      .sendWithEOM();
                }(registry, downstream, cancelToken)
            )
        )
            .start();
      }
  );

  adminServer.addRoute(
      "GET",
      "/network-metrics",
      [registry = std::move(registry)](
          auto /*req*/,
          auto /*body*/,
          auto* downstream,
          folly::CancellationToken cancelToken) {
        if (cancelToken.isCancellationRequested()) {
          return;
        }
        folly::IOBufQueue queue{folly::IOBufQueue::cacheChainLength()};
        folly::io::QueueAppender app(&queue, 2048);
        JsonWriter json(app);
        json.beginObject();
        json.key("clients");
        json.beginArray();
        const auto now = std::chrono::steady_clock::now();
        for (const auto& m : registry->clientNetworkMetrics()->snapshot()) {
          const auto ageMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                                 now - m.updatedAt)
                                 .count();
          json.beginObject();
          json.field("connection_id", m.connectionId);
          json.field("peer", m.peer);
          json.field("active", m.active);
          json.field("session_mapped", m.sessionMapped);
          json.field(
              "publishing",
              !m.publishedTracks.empty() || !m.publishedNamespaces.empty());
          json.field(
              "subscribing",
              !m.trackSubscriptions.empty() || !m.namespaceSubscriptions.empty());
          json.key("published_tracks");
          json.beginArray();
          for (const auto& track : m.publishedTracks) {
            json.strVal(track);
          }
          json.endArray();
          json.key("published_namespaces");
          json.beginArray();
          for (const auto& trackNamespace : m.publishedNamespaces) {
            json.strVal(trackNamespace);
          }
          json.endArray();
          json.key("track_subscriptions");
          json.beginArray();
          for (const auto& track : m.trackSubscriptions) {
            json.strVal(track);
          }
          json.endArray();
          json.key("namespace_subscriptions");
          json.beginArray();
          for (const auto& trackNamespace : m.namespaceSubscriptions) {
            json.strVal(trackNamespace);
          }
          json.endArray();
          json.field("sample_age_ms", static_cast<uint64_t>(std::max<int64_t>(0, ageMs)));
          json.field("app_limited", m.appLimited);
          json.field("ecn_capable", m.ecnCapable);
          json.field("srtt_us", m.srttUs);
          json.field("rttvar_us", m.rttVarUs);
          json.field("min_rtt_us", m.minRttUs);
          json.field("queue_delay_us", m.queueDelayUs);
          json.field("cwnd_bytes", m.cwndBytes);
          json.field("inflight_bytes", m.inflightBytes);
          json.field("writable_bytes", m.writableBytes);
          json.field("acked_rate_bps", m.ackedRateBps);
          json.field("pacing_rate_bps", m.pacingRateBps);
          json.field("lost_packets", m.lostPackets);
          json.field("retransmitted_packets", m.retransmittedPackets);
          json.field("ect0", m.ect0);
          json.field("ect1", m.ect1);
          json.field("ce", m.ce);
          json.field("ce_fraction", m.ceFraction);
          json.field("l4s_weight", m.l4sWeight);
          json.endObject();
        }
        json.endArray();
        json.endObject();
        proxygen::ResponseBuilder(downstream)
            .status(200, proxygen::HTTPMessage::getDefaultReason(200))
            .header("Content-Type", "application/json")
            .body(queue.move())
            .sendWithEOM();
      });
}

} // namespace openmoq::moqx::admin
