/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 * Originally from github.com/facebookexperimental/moxygen.
 * See deps/moxygen/LICENSE for the original license terms.
 *
 * Copyright (c) OpenMOQ contributors.
 */

#include "MoqxRelayTestFixture.h"

#include <algorithm>

namespace moxygen::test {

TEST_F(MoQRelayTest, ClientSessionMetadataTracksActivityLifecycle) {
  auto metrics = std::make_shared<openmoq::moqx::stats::ClientNetworkMetricsStore>();
  relay_->setClientNetworkMetricsStore(metrics);

  auto publisherSession = createMockSession();
  publisherSession->setTransportConnectionId("publisher-cid");
  metrics->registerSession("publisher-cid");
  metrics->put(openmoq::moqx::stats::ClientNetworkMetrics{
      .connectionId = "publisher-cid",
      .updatedAt = std::chrono::steady_clock::now(),
  });

  auto subscriberSession = createMockSession();
  subscriberSession->setTransportConnectionId("subscriber-cid");
  metrics->registerSession("subscriber-cid");
  metrics->put(openmoq::moqx::stats::ClientNetworkMetrics{
      .connectionId = "subscriber-cid",
      .updatedAt = std::chrono::steady_clock::now(),
  });

  doPublishNamespace(publisherSession, kTestNamespace);
  doPublishWithHandle(publisherSession, kTestTrackName, makePublishHandle());
  auto trackSubscription = subscribeToTrack(
      subscriberSession,
      kTestTrackName,
      createMockConsumer(),
      RequestID(1),
      /*addToState=*/false);
  const TrackNamespace otherNamespace{{"test", "other"}};
  auto namespaceSubscription =
      doSubscribeNamespace(subscriberSession, otherNamespace, /*addToState=*/false);

  auto findClient = [&](std::string_view connectionId) {
    auto clients = metrics->snapshot();
    return *std::find_if(clients.begin(), clients.end(), [&](const auto& client) {
      return client.connectionId == connectionId;
    });
  };

  auto publisher = findClient("publisher-cid");
  EXPECT_TRUE(publisher.sessionMapped);
  EXPECT_EQ(publisher.publishedNamespaces, std::vector<std::string>{"test/namespace"});
  EXPECT_EQ(publisher.publishedTracks, std::vector<std::string>{"test/namespace/track1"});

  auto subscriber = findClient("subscriber-cid");
  EXPECT_TRUE(subscriber.sessionMapped);
  EXPECT_EQ(subscriber.trackSubscriptions, std::vector<std::string>{"test/namespace/track1"});
  EXPECT_EQ(subscriber.namespaceSubscriptions, std::vector<std::string>{"test/other"});

  trackSubscription->unsubscribe();
  namespaceSubscription->unsubscribeNamespace();
  removeSession(publisherSession);

  publisher = findClient("publisher-cid");
  EXPECT_TRUE(publisher.publishedNamespaces.empty());
  EXPECT_TRUE(publisher.publishedTracks.empty());
  subscriber = findClient("subscriber-cid");
  EXPECT_TRUE(subscriber.trackSubscriptions.empty());
  EXPECT_TRUE(subscriber.namespaceSubscriptions.empty());

  removeSession(subscriberSession);
}

// Test: forwardChanged must not crash when called after the publisher has
// terminated (onPublishDone clears handle/upstream). We trigger forwardChanged
// via Subscriber::requestUpdate changing forward from true→false (1→0
// transition). The subscriber survives drain because it has an open subgroup.
TEST_F(MoQRelayTest, ForwardChangedAfterPublisherTermination) {
  auto publisherSession = createMockSession();
  auto subSession = createMockSession();

  doPublishNamespace(publisherSession, kTestNamespace);
  auto publishConsumer = doPublish(publisherSession, kTestTrackName);

  // Subscriber with forward=true (default)
  auto consumer = createMockConsumer();
  auto handle = subscribeToTrack(subSession, kTestTrackName, consumer, RequestID(0));
  ASSERT_NE(handle, nullptr);

  // Begin a subgroup so the subscriber has open subgroups and survives drain
  auto sg = createMockSubgroupConsumer();
  EXPECT_CALL(*consumer, beginSubgroup(0, 0, _, _))
      .WillOnce([&sg](uint64_t, uint64_t, uint8_t, bool) {
        return folly::makeExpected<MoQPublishError, std::shared_ptr<SubgroupConsumer>>(sg);
      });
  auto subgroupRes = publishConsumer->beginSubgroup(0, 0, 0);
  ASSERT_TRUE(subgroupRes.hasValue());

  // Publisher terminates — onPublishDone clears handle/upstream.
  // forwarder->publishDone sets draining and calls drainSubscriber, but the
  // subscriber has an open subgroup so it stays (receivedPublishDone_=true).
  EXPECT_CALL(*consumer, publishDone(_))
      .WillOnce(Return(folly::makeExpected<MoQPublishError>(folly::unit)));
  publishConsumer->publishDone(
      {RequestID(0), PublishDoneStatusCode::SUBSCRIPTION_ENDED, 0, "publisher ended"}
  );

  // Subscriber sends requestUpdate changing forward from true→false.
  // This calls removeForwardingSubscriber → forwardingSubscribers_ 1→0 →
  // forwardChanged on relay callback. forwardChanged accesses
  // subscription.upstream which was nulled by onPublishDone → crash.
  RequestUpdate update;
  update.requestID = RequestID(0);
  update.forward = false;
  auto task = handle->requestUpdate(std::move(update));
  auto res = folly::coro::blockingWait(std::move(task), exec_.get());
  EXPECT_TRUE(res.hasValue());

  // Clean up: reset the subgroup so subscriber can be fully removed
  EXPECT_CALL(*sg, reset(_)).Times(1);
  subgroupRes.value()->reset(ResetStreamErrorCode::CANCELLED);

  removeSession(publisherSession);
  removeSession(subSession);
}

// Bug: when a second subscriber with forward=true joins an existing PUBLISH-path
// subscription (causing a 0→1 forwarding transition), the relay fires REQUEST_UPDATE
// twice — once via forwardChanged() (which fires synchronously inside addSubscriber
// via addForwardingSubscriber) and once via the explicit block at the end of the
// subscribe() else-branch. Analogous to the subscribeNamespace bug fixed in this PR.
TEST_F(MoQRelayTest, Subscribe_SecondForwardingSubscriber_SingleRequestUpdate) {
  auto pubSession = createMockSession();
  doPublishNamespace(pubSession, kTestNamespace);
  auto mockHandle = makePublishHandle();
  doPublishWithHandle(pubSession, kTestTrackName, mockHandle);

  // S1 joins with forward=false — no REQUEST_UPDATE expected (no forwarding change).
  auto s1 = createMockSession();
  setupPublishSucceeds(s1);
  {
    SubscribeRequest sub;
    sub.fullTrackName = kTestTrackName;
    sub.requestID = RequestID(1);
    sub.locType = LocationType::LargestObject;
    sub.forward = false;
    withSessionContext(s1, [&]() {
      auto res = folly::coro::blockingWait(
          publisherInterface()->subscribe(std::move(sub), createMockConsumer()),
          exec_.get()
      );
      EXPECT_TRUE(res.hasValue());
      if (res.hasValue()) {
        getOrCreateMockState(s1)->subscribeHandles.push_back(*res);
      }
    });
  }
  for (int i = 0; i < 3; i++) {
    exec_->drive();
  }

  // Now expect exactly ONE REQUEST_UPDATE(forward=true) when S2 joins.
  // Before the fix this fires TWICE (forwardChanged + explicit block).
  EXPECT_CALL(*mockHandle, requestUpdateCalled(_)).Times(1).WillOnce([](const RequestUpdate& u) {
    ASSERT_TRUE(u.forward.has_value());
    EXPECT_TRUE(*u.forward);
  });

  auto s2 = createMockSession();
  setupPublishSucceeds(s2);
  {
    SubscribeRequest sub;
    sub.fullTrackName = kTestTrackName;
    sub.requestID = RequestID(2);
    sub.locType = LocationType::LargestObject;
    sub.forward = true;
    withSessionContext(s2, [&]() {
      auto res = folly::coro::blockingWait(
          publisherInterface()->subscribe(std::move(sub), createMockConsumer()),
          exec_.get()
      );
      EXPECT_TRUE(res.hasValue());
      if (res.hasValue()) {
        getOrCreateMockState(s2)->subscribeHandles.push_back(*res);
      }
    });
  }
  for (int i = 0; i < 5; i++) {
    exec_->drive();
  }

  // Verify before cleanup (cleanup legitimately sends forward=false).
  ASSERT_TRUE(testing::Mock::VerifyAndClearExpectations(mockHandle.get()));

  removeSession(s2);
  removeSession(s1);
  removeSession(pubSession);
  for (int i = 0; i < 3; i++) {
    exec_->drive();
  }
}

} // namespace moxygen::test
