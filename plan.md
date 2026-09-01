# Client Network Measurement and Bottleneck Control Plan

Last updated: 2026-08-31

## Purpose

Make `moqx` observe each client's network condition, detect whether that client is behind a bottleneck, detect clients likely sharing the same bottleneck, and eventually adapt media forwarding for those clients.

The relay already receives transport feedback from mvfst. This project turns that feedback into stable, per-client, application-usable information instead of relying on packet captures or router-only counters.

Main reasons:

- Router measurements show that congestion exists but do not tell the relay which MoQ session should be adapted.
- Peer IP and port are not stable client identities. Ports change after reconnects and several clients may share one IP through NAT.
- Raw cumulative ECN and loss counters do not describe current conditions. Detection needs recent deltas and trends.
- One RTT spike or CE mark is not enough evidence. Decisions need several samples and hysteresis.
- Clients sharing a bottleneck should often be handled together. Their addresses may differ, but their congestion signals should change at similar times.
- QUIC callbacks must remain lightweight. Detection and media actions must not block packet processing.

## Maintenance rule

Update this file whenever a planned item is completed, removed, reordered, or materially redesigned.

For every update:

1. Change item status.
2. Record important implementation files.
3. Record verification performed and result.
4. Add design changes to Decision Log.
5. Update `Last updated`.

Statuses used below:

- **DONE**: implemented and verified.
- **PARTIAL**: useful foundation exists, but listed work remains.
- **NEXT**: next implementation target.
- **PLANNED**: agreed work, not started.
- **LATER**: intentionally deferred until prerequisite evidence exists.

## Current architecture

```text
mvfst ACK/transport observers
    |
    v
ClientNetworkMetricsStore
    |
    +--> GET /network-metrics
    |
    v
short per-client history          [NEXT]
    |
    v
ClientNetworkController           [PLANNED]
    |
    +--> individual state/score
    +--> shared-bottleneck group
    +--> recommended action
    |
    v
connection_id -> MoQSession
    |
    v
session EventBase -> media action [LATER]
```

## Phase 0: Raw per-connection telemetry

Status: **DONE**

Implemented measurements:

- Smoothed RTT.
- RTT variance.
- Minimum RTT.
- Estimated queue delay: `max(srtt - min_rtt, 0)`.
- Congestion window.
- Inflight bytes.
- Writable bytes.
- Acknowledged delivery rate.
- Pacing rate when available.
- App-limited state.
- Lost and retransmitted packet counters.
- ECT(0), ECT(1), and CE counters echoed through QUIC ACK ECN feedback.
- ECN-capable state.
- CE fraction and mvfst L4S weight.
- Sample age and active state.

Implemented interface:

```text
GET /network-metrics
```

Primary files:

```text
src/stats/ClientNetworkMetrics.h
src/stats/ClientNetworkMetrics.cpp
src/stats/StatsRegistry.*
src/admin/MetricsHandler.cpp
```

Why this phase exists:

The relay needs transport measurements in one structured store. Logs and `tcpdump` are useful for debugging, but unsuitable as a control-loop input.

Verification completed:

- Native client sent ECT(1).
- Router DualPI2 produced CE markings under congestion.
- Relay received increasing ACK ECN CE counters.
- `/network-metrics` exposed ECT(1), CE, RTT, queue delay, congestion window, loss, and acknowledged rate.
- ECN-capable and non-ECN-capable cases appeared correctly in live output.

Known limitations:

- Several counters are cumulative over connection lifetime.
- Current CE fraction may describe only the latest observer update, not a stable analysis window.
- Latest sample alone cannot identify a trend.
- Very small measured minimum RTT is expected in a local virtual topology and is not representative of Internet propagation delay.

## Phase 1: QUIC connection to MoQ session mapping

Status: **DONE**

### Completed

- Propagate mvfst connection ID into `MoQSession` for native MoQT and H3 WebTransport paths.
- Register `connection_id -> weak_ptr<MoQSession>` in `MoqxRelayContext::onNewSession()`.
- Remove mapping when session ends.
- Prevent an old session close from deleting a replacement session's mapping.
- Provide `findSessionByConnectionId()`.
- Log mapping creation and removal using `SESSION_MAP`.
- Keep moxygen changes on fork branch `moqx-network-metrics`.
- Register mapped sessions in the existing thread-safe `ClientNetworkMetricsStore`.
- Track active published tracks and published namespaces per connection.
- Track active direct track subscriptions and namespace subscriptions per connection.
- Remove activity when its existing MoQ lifecycle handle ends and remove all metadata when the
  session mapping ends.
- Expose session mapping and activity through `/network-metrics`.
- Model publishing and subscribing as independent activity, allowing one session to do both.

Primary files:

```text
deps/moxygen/moxygen/MoQServer.cpp
deps/moxygen/moxygen/MoQServer.h
deps/moxygen/moxygen/MoQSession.h
deps/moxygen/moxygen/relay/MoQForwarder.h
deps/moxygen/moxygen/relay/MoQForwarder.cpp
src/MoqxRelayContext.h
src/MoqxRelayContext.cpp
src/MoqxRelay.h
src/MoqxRelay.cpp
src/stats/ClientNetworkMetrics.h
src/stats/ClientNetworkMetrics.cpp
src/admin/MetricsHandler.cpp
src/MoqxRelayServer.cpp
src/MoqxPicoRelayServer.cpp
test/MoqxRelayContextTest.cpp
test/MoqxRelaySubscribeTests.cpp
```

Why this mapping is needed:

The detector operates on a QUIC connection. Media adaptation operates on a MoQ session. Without this mapping, the relay can detect a congested connection but cannot safely find the subscriber whose media behavior must change.

Peer address cannot replace connection ID because:

- Client source ports change after reconnects.
- NAT can give several users one public IP.
- Connection migration may change peer address.
- An old session may close after a replacement is already active.

### Session metadata

Implemented application context:

```text
connection_id
peer
live MoQSession
publishing activity
subscription activity
track namespace/name or subscription identifier
```

Do not model publisher and subscriber as mutually exclusive roles. A MoQ session may both publish and subscribe. Store activity and active tracks/subscriptions instead.

Endpoint fields:

```json
{
  "connection_id": "...",
  "peer": "...",
  "session_mapped": true,
  "publishing": false,
  "subscribing": true,
  "published_tracks": [],
  "published_namespaces": [],
  "track_subscriptions": [],
  "namespace_subscriptions": []
}
```

Stable application/user identity remains intentionally outside Phase 1. No authenticated user ID
currently exists in the session API. Until one exists, `connection_id` identifies transport and the
activity arrays identify MoQ work. Do not invent identity from peer IP or port.

Verification completed:

- Live `SESSION_MAP add` connection IDs matched `/network-metrics` IDs for multiple clients.
- Live `ex.yaml` run exposed all three subscriber sessions with `session_mapped: true` and the
  expected catalog plus `video/s0`, `video/s1`, and `video/s2` direct subscriptions.
- The live roles were correct: all three connections were subscribing, none were publishing, and
  no namespace subscriptions were reported because these clients used direct track subscriptions.
- The topology/IP mapping also reproduced the expected ECN split: native subscribers `sub-r`
  (`10.99.0.4`) and `sub-2` (`10.99.0.6`) were ECN-capable, while Chrome subscriber `sub-1`
  (`10.99.0.5`) was not.
- Context test verifies registration, reconnect replacement, stale-close protection, and removal.
- Activity lifecycle test verifies publish namespace, publish track, direct subscribe, namespace
  subscribe, and every corresponding removal.
- All 6 `MoqxRelayContextTest` tests passed.
- All 38 `MoQRelayTest` tests passed.
- All 21 moxygen `MoQForwarderTest` tests passed.
- All 11 moxygen `OpenMOQForwarderTest` tests passed.
- `relay_chain` integration test passed.
- `admin_metrics_endpoint` integration test passed.

## Phase 2: Short per-client metric history

Status: **NEXT**

Extend `ClientNetworkMetricsStore` with a bounded history for every active connection.

Initial design:

```text
sample period: 100-250 ms
default controller period: 250 ms
history window: 10 seconds
maximum samples per connection: about 40 at 250 ms
```

Keep design bounded so inactive clients cannot grow memory indefinitely. Remove history after connection inactivity plus a short diagnostic retention period.

Each history sample should contain:

```text
timestamp
srtt_us
rttvar_us
min_rtt_us
queue_delay_us
new_ect0
new_ect1
new_ce
ce_fraction_window
new_losses
new_retransmissions
loss_rate_window
acked_rate_bps
cwnd_bytes
inflight_bytes
writable_bytes
pacing_rate_bps
app_limited
```

Calculate deltas from cumulative counters:

```cpp
newCe = current.ce >= previous.ce ? current.ce - previous.ce : 0;
newLosses = current.lostPackets >= previous.lostPackets
    ? current.lostPackets - previous.lostPackets
    : 0;
```

Counter decreases mean reset, replacement, or reconnect; they must never cause unsigned underflow.

Derived window measurements:

```text
CE fraction = new CE / (new ECT0 + new ECT1 + new CE)
queue-delay slope
RTT slope
loss rate
delivery-rate trend
CWND trend
writable-pressure duration
```

Samples should be ignored or given less weight when:

- Client is app-limited.
- Sample is stale.
- Too few packets were observed to make a ratio meaningful.
- Connection has insufficient history.
- Counter reset occurred.

Why this phase is needed:

Cumulative values answer “how much happened since connection start.” Bottleneck detection needs “what changed recently.” A bounded time series enables sustained-signal checks, slopes, correlations, and recovery detection.

Implementation preference:

- Reuse `ClientNetworkMetricsStore`; do not add a second unrelated metrics registry.
- Use a standard bounded container such as `std::deque` unless existing code offers a better local pattern.
- Keep raw collection independent from detection policy.
- Avoid new dependencies.

Verification required:

- Correct deltas across normal counter increases.
- No underflow after counter reset.
- Old samples leave the 10-second window.
- Inactive connection history is eventually removed.
- App-limited samples are identifiable.
- Window CE/loss values respond to router changes and later decay.

## Phase 3: ClientNetworkController

Status: **PLANNED**

Create:

```text
src/control/ClientNetworkController.h
src/control/ClientNetworkController.cpp
```

Run approximately every 250 ms outside mvfst packet callbacks, using a control/dedicated EventBase or an existing safe periodic execution facility.

Initial loop:

```cpp
auto clients = metricsStore->snapshot();
updateHistories(clients);
detectIndividualBottlenecks();
groupSharedBottlenecks();
publishRecommendations();
```

Do not apply media changes in first controller version. Produce logs and endpoint recommendations until detection is validated.

Why separate controller from QUIC callback:

- ACK processing must remain fast.
- Grouping compares several clients and is not packet-local work.
- Media operations may need another EventBase.
- Separating measurement, policy, and action makes experiments repeatable.

Controller lifecycle requirements:

- Start and stop with relay cleanly.
- Never outlive metrics store or relay context.
- Do not retain strong session references unnecessarily.
- Skip stale/inactive connections.
- Bound work per cycle.

## Phase 4: Individual bottleneck detection

Status: **PLANNED**

Use a state machine:

```text
NORMAL
SUSPECTED
CONGESTED
RECOVERING
```

Evidence of a bottleneck:

```text
app_limited == false
sustained or increasing queue delay
sustained CE fraction
writable_bytes == 0 for several samples
CWND shrinking or remaining constrained
acknowledged delivery rate capped or falling
loss rate increasing
```

No single signal is sufficient in every environment:

- CE is strong evidence only for ECN-capable traffic.
- Loss can be congestion or random link loss.
- RTT can change because of routing or wireless variation.
- Low delivery rate can mean low application demand.
- `writable_bytes == 0` matters most when not app-limited.

Initial behavior:

- Require several consecutive samples before entering `SUSPECTED`.
- Require sustained evidence before entering `CONGESTED`.
- Enter `RECOVERING` when evidence weakens.
- Return to `NORMAL` only after a longer stable period.
- Use hysteresis so media quality does not oscillate.

Produce:

```text
bottleneck_state
bottleneck_score in [0, 1]
reason flags
state age
```

Possible initial score inputs, subject to experiment calibration:

```text
queue-delay level and slope
CE fraction
loss rate
writable/CWND pressure
delivery-rate reduction
```

Weights and thresholds must remain explicit and testable. They are experimental parameters, not universal Internet constants.

Verification scenarios:

- Uncongested, app-limited stream remains `NORMAL`.
- Bandwidth cap below offered bitrate reaches `CONGESTED`.
- ECN-capable DualPI2 case reacts to CE.
- Non-ECN case can react using delay/loss/rate evidence.
- Brief spike does not cause persistent congestion state.
- Removing bottleneck produces `RECOVERING`, then `NORMAL`.

## Phase 5: Shared-bottleneck grouping

Status: **PLANNED**

Compare synchronized recent histories between active clients.

Signals to correlate:

```text
queue-delay changes
CE bursts
loss bursts
delivery-rate reductions
recovery timing
```

Why timing matters:

Clients traversing the same congested queue should experience related changes during the same periods. Same IP is neither required nor sufficient because clients can share NAT without sharing the full path, or share a downstream queue while using different addresses.

Minimal first algorithm:

1. Place samples into fixed time buckets.
2. Normalize recent signal changes per client.
3. Compute pairwise correlation only when both clients have enough non-app-limited data.
4. Mark pairs above a conservative threshold.
5. Form groups using connected components.
6. Require persistence before changing group membership.

Avoid ML initially. Add more complex clustering only if controlled experiments show simple correlation is inadequate.

Produce:

```text
shared_group
group_confidence
correlated_signals
group age
```

Verification topology:

- Clients A and B traverse one shaped bottleneck.
- Client C traverses a separate shaped link.
- Change shared link bandwidth or load during playback.
- A and B should group after sustained correlated evidence.
- C should remain separate.
- Remove congestion and verify group confidence decays.

## Phase 6: Endpoint and observability extension

Status: **PLANNED**

Extend `/network-metrics` without removing raw measurements.

Target shape:

```json
{
  "connection_id": "...",
  "peer": "...",
  "active": true,
  "session_mapped": true,
  "ce_total": 455,
  "ce_window": 73,
  "ce_fraction_window": 0.34,
  "lost_packets_total": 235,
  "lost_packets_window": 8,
  "loss_rate_window": 0.02,
  "queue_delay_us": 29768,
  "queue_delay_slope": 0.21,
  "bottleneck_state": "congested",
  "bottleneck_score": 0.94,
  "bottleneck_reasons": ["sustained_ce", "queue_delay", "cwnd_limited"],
  "shared_group": 2,
  "group_confidence": 0.87,
  "recommended_action": "lower_video_layer",
  "applied_action": "none"
}
```

Clearly name total and window values. Do not expose ambiguous `ce_fraction` once both meanings exist.

Add concise transition logs rather than logging every 250 ms:

```text
state transition
shared-group change
recommended-action change
applied-action change/failure
```

## Phase 7: Recommendation-only media policy

Status: **PLANNED**

Convert detector state into a recommended action without changing forwarding behavior.

Possible recommendations:

```text
keep current layer
select lower video layer
disable enhancement layer
reduce subscription priority
drop stale groups/objects
apply relay sending budget
restore higher layer
```

Why recommendation-only comes first:

It allows comparison against video behavior and router ground truth without risking oscillation, accidental starvation, or incorrect adaptation from an immature detector.

Record recommendation, reason, timestamp, and duration through endpoint and transition logs.

## Phase 8: Safe media action dispatch

Status: **LATER**

Required flow:

```text
controller result
    -> connection_id lookup
    -> weak MoQSession validation
    -> dispatch to session's owning EventBase
    -> apply action
    -> record success/failure
```

Never mutate session or forwarding state directly from:

- mvfst observer callback.
- metrics-store lock scope.
- controller thread when session belongs to another EventBase.

Start with one reversible action: select a lower video layer or stop forwarding an enhancement layer. Add recovery action to restore quality only after stable conditions.

Safety requirements:

- Session may disappear between detection and dispatch.
- A reconnect may replace connection mapping.
- Repeated identical recommendations must not repeatedly apply the same action.
- Add minimum hold time before another quality change.
- Recovery should be slower than congestion response.
- Base/audio layer should not be removed accidentally.

## Phase 9: Testing and experiment plan

Status: **PLANNED THROUGHOUT ALL PHASES**

### Unit tests

```text
counter delta calculation
counter reset handling
history eviction
inactive-client cleanup
stale-sample filtering
app-limited filtering
state transitions
hysteresis
session replacement/removal
pair correlation
group creation/removal
action deduplication
```

### Integration tests

```text
one uncongested client
one congested client
two clients sharing one bottleneck
two clients behind separate bottlenecks
client disconnect and reconnect
ECN-capable native client
non-ECN-capable client
loss-only impairment
latency/jitter changes without congestion
bandwidth change during active stream
```

### Experiment matrix

```text
bandwidth: 1, 2, 4, 10, 15 Mbps
base latency: 0, 20, 100, 300 ms
jitter: 0, 5, 20, 50 ms
loss: 0, 0.1, 1, 5, 10 percent
ECN: enabled and disabled
clients: 1, 2, 4 or more
```

Use router ground truth:

```bash
docker exec mn.router tc -s qdisc show dev router-eth1
docker exec mn.router tc -s class show dev router-eth1
```

Record:

```text
time impairment changed
router queue backlog
DualPI2 CE marks/drops
relay window metrics
detector transition time
shared-group assignment
recommendation/action time
video stalls and selected layer
```

The goal is not merely detecting configured impairment. The detector must distinguish an active bottleneck from high baseline latency, random loss, or an application-limited sender.

## Ordered implementation checklist

- [x] Collect raw per-connection QUIC telemetry.
- [x] Expose raw telemetry at `/network-metrics`.
- [x] Validate ECT(1) and CE feedback using native client.
- [x] Map QUIC connection ID to live `MoQSession`.
- [x] Protect mapping across reconnect and stale close.
- [x] Push transport-hook and connection-identity moxygen changes to maintained fork.
- [x] Commit parent repository's `.gitmodules` and moxygen submodule pointer.
- [ ] Commit and push Phase 1 `MoQForwarder` lifecycle callbacks, then update parent submodule
  pointer.
- [ ] Add bounded per-client history.
- [ ] Add cumulative-counter delta calculations.
- [ ] Add window-derived measurements.
- [ ] Expose total versus window metrics clearly.
- [x] Add publishing/subscription metadata to mapped session context.
- [ ] Add logging-only `ClientNetworkController`.
- [ ] Add individual bottleneck state machine and score.
- [ ] Expose state, score, and reason flags.
- [ ] Validate individual detection across controlled impairments.
- [ ] Add shared-bottleneck grouping.
- [ ] Validate shared versus separate bottlenecks.
- [ ] Add recommendation-only media policy.
- [ ] Validate recommendations against video behavior.
- [ ] Dispatch one reversible media action through session EventBase.
- [ ] Add recovery and quality restoration.
- [ ] Tune thresholds from experiment data.

## Decision log

### 2026-08-31: Use relay-side QUIC telemetry

Router counters remain experiment ground truth, but relay-side transport feedback drives detection because only the relay can connect congestion evidence to a MoQ session and media action.

### 2026-08-31: Use connection ID, not peer port, as transport key

Peer ports change and addresses may be shared. QUIC connection ID provides the transport/session join key already available to metrics collection.

### 2026-08-31: Keep weak session references

Metrics/control infrastructure must not extend session lifetime. Mapping stores weak references and validates them before action.

### 2026-08-31: Use recent deltas and trends

Cumulative ECN/loss counters are retained for diagnostics, but decisions will use bounded-window deltas and time trends.

### 2026-08-31: Start with simple heuristics

Use explicit state machine, hysteresis, and pairwise correlation before considering ML. This keeps behavior explainable and gives experiments a clear baseline.

### 2026-08-31: Separate recommendation from action

Detector will first emit state, score, grouping, and recommended action. Actual media adaptation remains disabled until controlled experiments validate behavior.

### 2026-08-31: Patch moxygen through maintained fork

`moq-testbed` points to fork branch containing required transport hooks and connection identity propagation because upstream repository is not directly writable.

### 2026-08-31: Track subscription lifecycle at MoQForwarder

Use default no-op `subscriberAdded` and `subscriberRemoved` callbacks on `MoQForwarder::Callback`.
This preserves the existing concrete subscription handle and observes every removal path instead of
wrapping handles or duplicating unsubscribe logic in moqx.

### 2026-08-31: Keep transport identity separate from user identity

Phase 1 maps connection, session, peer, and MoQ activity. A stable user ID will only be added when
protocol authentication or application metadata provides one; peer IP and port are not substitutes.
