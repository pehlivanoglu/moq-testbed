# QUIC shared bottleneck simulation

A separate ns-3/picoquic workflow for evaluating server-side
`PracticalPassiveAndRFC`. Runs require no Docker, Containernet, network
namespaces, real network sockets, video files, browser player, or MoQT.
The topology schema, designer assets, and existing C++ SBD core are reused.

Run commands from `moqlab/` with Python 3.11+:

```bash
python3 -m venv simulation/.venv
source simulation/.venv/bin/activate
python -m pip install -r simulation/requirements-dev.txt
python -m simulation doctor
python -m simulation build --jobs 8
python -m simulation design -c simulation/examples/shared.yaml
python -m simulation validate -c simulation/examples/shared.yaml
python -m simulation run -c simulation/examples/shared.yaml
python -m simulation analyze simulation/.runs/<archive>
python -m simulation verify
```

Build prerequisites on Ubuntu: Git, CMake 3.22+, GCC 13+ with C++23 support,
OpenSSL development headers, and Python development tooling. The build fetches
locked source revisions, applies recorded patches, runs entirely under
`simulation/.build/`, and does not install system software or touch emulation.
Network access is needed for dependency preparation, not built experiments.
Build logs remain in `.build/`. `doctor` distinguishes missing build tools from
an unbuilt simulator. Existing modified dependency checkouts are not reset.

For independent repeat parallelism, use `run --jobs 8`. Configuration owns seed,
repetitions, topology, and experiment parameters; `--jobs` only controls host
resource use. `--output PATH` chooses a new archive directory and refuses to
replace an existing one. A failed run retains partial output and a failed
manifest; analysis refuses incomplete archives.

## Designer and document

`simulation design` opens the existing graph editor in simulation mode. Its
Experiment inspector edits transport, workloads, connections, queues, routes,
feedback, evaluation, and timed events. Object collections have Add/Remove
controls; event entries have a type selector. Primitive paths accept
comma-separated node ids. Node overrides use node ids; queue overrides use
`source->target`; connection timing uses `sender->receiver` or `external:id`.
Undo/redo and import/export include experiment settings. Simulation autosaves
use a separate storage key. The designer binds only to localhost and exports
files; it does not launch experiments.

A single YAML document contains:

```yaml
kind: simulation
schema_version: 1
topology:
  # Existing moqlab topology: relays, publishers, subscribers, routers,
  # switches, traffic, and directed links.
experiment:
  duration_s: 60
  seed: 1
  repetitions: 1
  transport:
    congestion_control: cubic  # or bbr
    ecn: ect0                 # off / ect0 / ect1
  workload:
    kind: paced               # bulk / paced / segmented
    rate_mbps: 7
  queue:
    kind: fifo                # fifo / red / pie / fq_codel / dualpi2
    limit_packets: 100
  connections:
    server->client-b: {start_s: 5, stop_s: 50}
  events:
    - kind: link
      time_s: 40
      source: router
      target: switch
      bandwidth_mbps: 6
```

`SimulationConfig` provides the complete validated schema. Start with bundled
`shared.yaml`, `private.yaml`, `uncongested.yaml`, or `route-change.yaml`.
`shared-red.yaml` repeats the shared example with only the default queue kind
changed to RED (100-packet limit, min/max thresholds 20/60 packets, ECN on).
`shared-dualpi2.yaml` changes only that kind to DualPI2 (15 ms target,
1 ms step, 16 ms update, coupling 2, 150,000-byte limit). It retains CUBIC and
ECT(0), so this is a classic-traffic comparison, not an L4S workload.
`shared-light.yaml` uses a lighter 3.9 Mbps origin load, a 40-packet queue
limit, and 120 simulated seconds. The completed two-controller/five-AQM,
two-seed/two-run matrix is documented in
[experiments/light-bottleneck/README.md](experiments/light-bottleneck/README.md).
`benchmark-cubic.yaml` and `benchmark-bbr.yaml` each specify 100 measured
connections, 300 simulated seconds and 30 independent repetitions.

Legacy topology YAML can be imported. Publisher means byte origin; relay means
QUIC server/forwarder; subscriber means byte client. Image, listen/admin ports,
asset, namespace/track, browser, playback, startup warmups, and TLS settings in
that shared schema are compatibility metadata, not simulated media behavior.
Simulation uses its own transport/application settings and certificate fixture.
Active media caches and algorithms other than `PracticalPassiveAndRFC` are
rejected. Enable `sbd.enabled` on the server relay and retain its required
`edge: true` flag. Simulation does not enable a detector implicitly.

## Modeled behavior

- Each endpoint embeds real picoquic, including encrypted handshake, reliable
  streams, ACKs, loss recovery, retransmissions, flow control, pacing and CUBIC
  or picoquic's `bbr.c` implementation (BBRv3-derived; not `bbr1.c`, Linux BBR,
  or mvfst BBR). Exact dependency revisions are archived.
- Reliable stream 3 carries synthetic data. Origins offer saturated bulk,
  paced bytes, or scheduled segments; no codec or HTTP model is involved.
- Relays forward only bytes received upstream and verify byte identity at every
  receiver. Each required downstream connection starts at byte zero. A late
  required child can backpressure the origin. Application buffering is bounded
  by `relay_buffer_bytes`; QUIC also retains transport state. Closing a child
  removes it from required fan-out. Stop events stop generation; accepted data
  may drain. This coupling is intentional and can affect SBD observations.
- The dedicated receiver stream 2 returns deduplicated QUIC packet numbers and
  receive timestamps in fixed 16-byte records. Feedback consumes real reverse
  path capacity and is subject to QUIC recovery and flow control. The sender
  joins feedback with its own send ledger. It cannot read receiver/queue truth
  directly. This is an explicit feedback extension, not a claim that standard
  QUIC ACKs contain receiver timestamps.
- Detector equations come directly from `src/sbd/`. Receiver intervals are
  350 ms; estimator warm-up is 50 intervals; grouping needs 100, plus the
  discarded initial partial interval. Empty intervals reset warm-up. Outcomes
  use transport-declared packet loss and spurious-loss correction, not link
  drop notifications. Decisions use the latest completed common interval.
  Loss declarations follow picoquic's packet-loss events, including its
  timer-based declarations; an eventual ACK corrects a spurious declaration.
- Network forwarding uses ns-3 IPv4 and transparent MAC bridges. Each directed
  link has a custom `SimpleNetDevice` subclass with one packet in service and
  no waiting device queue; native ns-3 queue disciplines own backlog. Device
  backpressure wakes the queue discipline. Sender-local packet tags are not
  transported to the next hop; IP ECN bits and actual packet bytes are.
- Serialization includes `wire_overhead_bytes` (default 38 bytes per IP packet:
  Ethernet header/FCS/preamble/gap approximation). No collision or NIC/CPU
  scheduling model is included. Link capacity defaults explicitly to 1000 Mbps.
  `link_mtu_bytes` defaults explicitly to 1500; this also calibrates native AQM
  models. Transport `mtu_bytes` is passed to picoquic's `mtu_max` API, not a
  promise of datagram length: initial sizing and probing follow the pinned
  stack. Validation reserves an additional conservative 28-byte headroom.
  Application generation/forwarding advances at 1 ms; transport wakeups are
  event driven and use picoquic's microsecond clock.
- Independent uniform jitter is clipped at zero propagation delay and may
  reorder packets. Loss is an independent per-transmission Bernoulli process.
  This is not claimed to reproduce Linux netem distribution semantics.
- FIFO, RED, PIE and FQ-CoDel use ns-3 models. DualPI2 uses the GPRT model carried
  by the pinned Stratum ns-3 patches. Its `limit_packets` is converted to a
  byte limit using 1500 bytes per packet-equivalent; other queues use packet
  limits. Router AQM settings apply to all its egresses unless an experiment
  queue override selects that directed link. Record ECN mode separately from
  controller behavior: ECT(1) does not make CUBIC/BBR L4S compliant.
- Existing bulk TCP, paced CBR UDP and segmented TCP loads use named paths and
  isolated address pairs. TCP is ns-3 TcpCubic with ECN. `quic_bulk` adds
  synthetic QUIC background transfers with explicit ids and endpoints.
- Scheduled link edits preserve queued and in-flight packets. A link disabled
  by an event drops new transmissions without implicitly changing routing.
  Explicit route changes retain endpoint addresses/connections and apply to
  future forwarding; packets already scheduled on a link finish that link.
  Existing SBD history remains valid until its normal gap/staleness policy
  invalidates it. This models an IP path change, not multipath QUIC.
- Queue replacement rejects nonempty backlog unless `backlog_policy: flush`
  explicitly records the discarded queue. The packet already being serialized
  completes; old queue controller timers are disposed.

## Outputs and interpretation

Every archive has original and resolved YAML, a provenance manifest, dependency
and source hashes, and one `run-NNNN/` per repetition. Each native run records
`events.jsonl`, `applications.jsonl`, `queues.jsonl`, `sbd.jsonl`, and
`truth.jsonl`. The latter stores paths and active queues compactly so the
pairwise reference can be reconstructed without quadratic trace output.
A `COMPLETE` marker is written after native accounting and output flush checks;
a second marker at the archive root means all repetitions completed.

`trace: packets` adds enqueue/dequeue/drop/mark events, actual receiver timestamps,
feedback arrival/watermarks, and transport outcomes. `sbd-replay` independently
reconstructs interval summaries from these observed inputs. `pcap: true` writes
per-direction DLT_RAW IPv4 captures; `qlog: true` writes picoquic qlogs. These
options can significantly increase time and storage use. Packet payloads are
synthetic; TLS key logs are not an experiment output.

`analyze` creates `analysis.json`, per-run `metrics.csv`, a copy of its analysis
source, and an overview PNG. It reports pairwise
structural sharing separately from an operational active-congestion reference
(utilization, persistent backlog fraction, and mean queue sojourn thresholds).
FQ-CoDel truth refers to shared root service capacity; isolated per-flow inner
queues can produce different delay patterns despite that shared capacity.
Raw queue observations and events permit alternate thresholds offline.
Active sharing uses observed per-flow queue traversal and residence, including
packets still following an old path after a route change. Structural sharing
uses the configured route. Service utilization integrates the in-service
packet across observation windows, including very slow links.

Scores distinguish evidence interval from decision time, exclude unknown/
warm-up/stale pairs from confusion matrices, and report their coverage cost.
Route-transition windows are counted separately. Precision, recall, F1, false
positive rate, detection/recovery latency, and unresolved transitions are
reported. Confidence intervals resample independent runs, not correlated time
samples; one repetition has no confidence interval. Latency resolution is the
configured evaluation window. Poor detector accuracy is reported as an
observation, not converted into a simulator test failure.
Structural sharing is a topology reference, not a claim that an uncongested
shared path should be detected as a bottleneck. Sensitivity profiles jointly
scale active-reference thresholds by the configured factors (default 0.8/1/1.2),
capping fractional thresholds at one; detector thresholds are unchanged.

The baseline assumes an ideal shared virtual clock. A deterministic protocol
RNG is used only inside this isolated simulator; TLS certificate verification
is disabled for the fixture certificate. Ciphertexts need not be reproducible,
but same-seed semantic traces are checked. These settings must not be reused
for a real network service. See [VALIDATION.md](VALIDATION.md) for actual checks,
measured scale, remaining limits and pre-existing repository test failures.
