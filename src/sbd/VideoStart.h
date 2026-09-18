/* SPDX-License-Identifier: Apache-2.0 */
#pragma once
#include "sbd/Service.h"
#include <moxygen/MoQFilters.h>
#include <moxygen/MoQSession.h>

namespace openmoq::moqx::sbd {
// Observation only: every result/payload is passed through unchanged.
class VideoSubgroup final : public moxygen::SubgroupConsumerFilter {
 public:
  VideoSubgroup(std::shared_ptr<moxygen::SubgroupConsumer> consumer, std::shared_ptr<Flow> flow)
      : SubgroupConsumerFilter(std::move(consumer)), flow_(std::move(flow)) {}
  folly::Expected<folly::Unit, moxygen::MoQPublishError> object(
      uint64_t id, moxygen::Payload payload, moxygen::Extensions extensions,
      bool fin) override {
    const bool data = payload && payload->computeChainDataLength();
    auto result = downstream_->object(id, std::move(payload), std::move(extensions), fin);
    if (data && result.hasValue()) flow_->videoStarted = true;
    return result;
  }
  folly::Expected<folly::Unit, moxygen::MoQPublishError> beginObject(
      uint64_t id, uint64_t length, moxygen::Payload payload,
      moxygen::Extensions extensions) override {
    const bool data = payload && payload->computeChainDataLength();
    auto result = downstream_->beginObject(id, length, std::move(payload), std::move(extensions));
    if (data && result.hasValue()) flow_->videoStarted = true;
    return result;
  }
  folly::Expected<moxygen::ObjectPublishStatus, moxygen::MoQPublishError> objectPayload(
      moxygen::Payload payload, bool fin) override {
    const bool data = payload && payload->computeChainDataLength();
    auto result = downstream_->objectPayload(std::move(payload), fin);
    if (data && result.hasValue()) flow_->videoStarted = true;
    return result;
  }
 private:
  std::shared_ptr<Flow> flow_;
};
class VideoTrack final : public moxygen::TrackConsumerFilter {
 public:
  VideoTrack(std::shared_ptr<moxygen::TrackConsumer> consumer, std::shared_ptr<Flow> flow)
      : TrackConsumerFilter(std::move(consumer)), flow_(std::move(flow)) {}
  folly::Expected<std::shared_ptr<moxygen::SubgroupConsumer>, moxygen::MoQPublishError>
  beginSubgroup(uint64_t group, uint64_t subgroup, moxygen::Priority priority, bool last) override {
    auto result = downstream_->beginSubgroup(group, subgroup, priority, last);
    if (!result || flow_->videoStarted) return result;
    return std::make_shared<VideoSubgroup>(std::move(*result), flow_);
  }
  folly::Expected<folly::Unit, moxygen::MoQPublishError> objectStream(
      const moxygen::ObjectHeader& header, moxygen::Payload payload, bool last) override {
    const bool data = payload && payload->computeChainDataLength();
    auto result = downstream_->objectStream(header, std::move(payload), last);
    if (data && result.hasValue()) flow_->videoStarted = true;
    return result;
  }
  folly::Expected<folly::Unit, moxygen::MoQPublishError> datagram(
      const moxygen::ObjectHeader& header, moxygen::Payload payload, bool last) override {
    const bool data = payload && payload->computeChainDataLength();
    auto result = downstream_->datagram(header, std::move(payload), last);
    if (data && result.hasValue()) flow_->videoStarted = true;
    return result;
  }
 private:
  std::shared_ptr<Flow> flow_;
};
inline std::shared_ptr<moxygen::TrackConsumer> watchVideo(
    std::shared_ptr<moxygen::TrackConsumer> consumer, const moxygen::FullTrackName& track,
    const std::shared_ptr<moxygen::MoQSession>& session, const std::shared_ptr<Service>& service) {
  // ponytail: the testbed's LOC/CMSF video naming; use catalog media types if
  // arbitrary publisher track names are introduced.
  if (!service || !service->config().enabled || !session ||
      (track.trackName != "video" && !track.trackName.starts_with("video/"))) return consumer;
  auto flow = service->attach(session->getTransportConnectionId(), session->getPeerAddress().describe());
  if (flow->videoStarted) return consumer;
  return std::make_shared<VideoTrack>(std::move(consumer), std::move(flow));
}
} // namespace openmoq::moqx::sbd
