# Simulation validation — 2026-10-22

The approved implementation is complete. Internal transport, network,
measurement, designer and scale checks passed. This establishes the tested
model's behavior, not equivalence to a physical network. The large reference
workload exposes very low decision coverage in the existing SBD implementation;
it does **not** establish useful detector accuracy at 100 connections.

Machine-readable evidence: [validation/results.json](validation/results.json).
Installation, model assumptions and commands: [README.md](README.md).
Generated archives below are local, ignored build/experiment artifacts, not
files intended for version control. Keep them when retaining research results.

## Delivered scope

- Research objective is server-side SBD. Existing `PracticalPassiveAndRFC`
  equations are compiled directly; other SBD algorithms are excluded.
- Real picoquic CUBIC and `bbr.c` (BBRv3-derived), encrypted reliable streams,
  ACK/loss/ECN handling, synthetic bulk/paced/segmented bytes; no MoQT or media.
  Extra congestion controllers remain deferred.
- Origins, finite-buffer forwarding relays, required-child fan-out/backpressure,
  clients, routers, transparent switches, directional network impairments,
  external bulk TCP/CBR UDP/segmented TCP and QUIC bulk.
- FIFO, RED, PIE, FQ-CoDel and stratum DualPI2; scheduled link, queue,
  connection and explicit route changes. ECT(1) and successful ACK_ECN
  transport do not make either selected congestion controller L4S compliant.
- Sender learns packet OWD through receiver timestamp feedback carried on a
  reliable reverse stream. Independent queue/path observations supply the
  evaluation reference only. Ideal shared clock is an explicit assumption.
- Existing designer edits physical topology **and** simulation settings in one
  YAML, including workloads, seeds, queues, routes and timed events.
  Simulation has a separate CLI, build tree, archives and browser storage;
  shared topology/designer/SBD code is reused. Emulation is not a dependency.

## Exact implementation and provenance

| Component | Revision |
| --- | --- |
| ns-3 3.48 | `d2add90b452d600cfb4859baed8e9ea633519447` |
| picoquic | `272fefbe76601f9fcc894e340a83ecde27768ca4` |
| stratum | `131e40cc8b9e83e9ad4e90652e6c298506ad3edf` |
| picotls | `f06553b877ada93c860dd1db9002eabf7365292d` |

The complete lock, including nested source/header hashes, is in
[deps.lock.json](deps.lock.json). Native sources, existing SBD sources, build
configuration, dependency patches and binary hashes are recorded in each run
manifest. `native-sources.zip` preserves the exact native source inputs.
The final benchmark archives' native source hashes, ZIP contents, binary hash,
30 completion markers each and current analysis-source hash were checked at
delivery. Analysis has its own archived source and hash because offline scoring
was corrected after generating the raw traces; traces were not regenerated or
edited to improve scores.

Both final benchmarks used binary SHA256
`88bf8c2e4e30b9d2d181857f8a3b9d03fa0408ea8a34114131a642bb1dfc19ef`.
Analysis SHA256 is
`febb8a8400bd8e8a1e5227649aa6f2dbc0a5e677862f057ef52b05051066fb5d`.
Earlier exploratory archives are development data and are not pooled here.

## Passed checks

Latest complete verification:
[verification.json](.runs/verification-20261022T044944.050499Z/verification.json).
Reproduce with `python -m simulation verify` after building.

| Check | Observed result |
| --- | --- |
| Python schema/API/scoring/isolation | 19 passed |
| Native independent SBD calculations and analytic wire model | 2 CTests passed |
| Patched picoquic upstream suite | 696 tests, 0 failures |
| ns-3 suites | DualPI2, stratum L4S, RED, PIE, CoDel passed |
| End-to-end configurations | 28 passed |
| Feedback replay through SBD | 24 interval summaries matched |
| Designer in real headless Chrome | forms, event variant, undo/redo, validation, YAML round trip passed |
| Same seed and repetition | semantic traces identical in repeat-shared case |

The 28 cases include shared/uncongested/private paths, route change, both
controllers across all five AQMs, ECT(1), PCAP/qlog instrumentation, compatible
parameter edits, explicit queue flush, segmented and external QUIC workloads,
feedback impairment, flow churn, tandem and overlapping sharing, correlated
independent cross traffic, all three existing external traffic kinds, and a
same-seed repeat. Every configured flow must deliver bytes. Tests validate
transport/model/accounting behavior, not an arbitrary minimum detector score.
FQ-CoDel is covered through end-to-end integration; no separate upstream
FQ-CoDel suite is claimed. Upstream model tests cover the pinned implementations,
not equivalence to Linux `tc`.

Analytic wire assertions cover serialization, propagation, a timed rate/delay
change, in-service occupancy across observation windows, MTU 1500, ns-3 node
context and local packet-tag isolation across hops. AQM owns the waiting
backlog; each device has only one packet in service. Instrumentation checks
include CE reaching the sender through QUIC ACK_ECN, populated PCAP/qlog,
transport loss and spurious-loss records, and replay of actual timestamp
feedback. Core checks independently calculate interval mean/extrema/loss,
warm-up and gap behavior rather than comparing the implementation to itself.

In the diagnostic instrumentation case, 3,627 server-side feedback records had
receive-to-sender observation delay median **24.006 ms**, p95 **33.904 ms**.
This is a modeled feedback-stream delay, not a measured difference from
mvfst ACK timestamp extensions. That case generated 36.65 MiB in 0.379 s wall
time (single short diagnostic run); it is not a full-tracing 100-flow benchmark
or an estimate of tracing overhead relative to the large reference workload.

## Completed reference workload

Each controller completed **100 measured downstream connections × 300 simulated
seconds × 30 repetitions**, plus the upstream QUIC connection. Configuration:
[CUBIC](examples/benchmark-cubic.yaml), [BBR](examples/benchmark-bbr.yaml).
The benchmark uses paced 0.5 Mbps per child with a shared 20 Mbps FIFO
bottleneck, bounded lossless fan-out, MTU 1500, summary tracing and no PCAP/qlog.

| Controller | Completed repetitions | Median wall time/repetition | Maximum RSS/repetition | Raw output, all repetitions |
| --- | ---: | ---: | ---: | ---: |
| CUBIC | 30 | 84.21 s | 822.08 MiB | 2.776 GiB |
| BBR | 30 | 66.26 s | 147.69 MiB | 2.839 GiB |

Measured on this Linux x86_64 host with 24 logical CPUs and **16 concurrent
independent repetitions**. Wall time includes contention and is not isolated
single-core performance. RSS is the maximum for one native process, not total
batch memory. Seed is 1; ns-3 run numbers 1–30 and protocol RNG state distinguish
repetitions. A fixed analysis seed, 20261021, drives 2,000 bootstrap resamples
of independent run means, never individual correlated time samples.

Final archives:

- [CUBIC analysis](.runs/final-benchmark-cubic/analysis.json),
  [CSV](.runs/final-benchmark-cubic/metrics.csv),
  [plot](.runs/final-benchmark-cubic/overview.png).
- [BBR analysis](.runs/final-benchmark-bbr/analysis.json),
  [CSV](.runs/final-benchmark-bbr/metrics.csv),
  [plot](.runs/final-benchmark-bbr/overview.png).

## Detector outcome and interpretation

Decision-time active-sharing results below are **conditional on known pair
decisions**. `n` is the number of independent runs with a defined ratio;
undefined values are not filled with zeros. Coverage includes unknown pairs
in its denominator. Warm-up/staleness cannot silently become true negatives.

| Metric: mean [95% CI], contributing n | CUBIC | BBR |
| --- | --- | --- |
| Coverage | 0.160% [0.121%, 0.202%], 30 | 0.140% [0.099%, 0.180%], 30 |
| Precision | 0.0465 [0.0228, 0.0764], 24 | 0.4878 [0.3435, 0.6299], 25 |
| Recall | 0.0522 [0.0247, 0.0840], 26 | 0.3099 [0.2281, 0.3932], 25 |
| F1 | 0.0369 [0.0189, 0.0604], 26 | 0.3561 [0.2596, 0.4504], 25 |
| False-positive rate | 0.0221 [0.0107, 0.0372], 26 | 0.0333 [0.0170, 0.0568], 25 |

**More than 99.8% of pair observations are unknown. These conditional scores
cannot support a claim of useful SBD accuracy at this scale, or a general
ranking of congestion controllers.** Existing 50/100-interval warm-up,
empty-interval reset, latest common interval, congestion/loss and fan-out
backpressure are visible in raw state; isolating their causal contributions
requires separate experiments. Detector equations and thresholds were not
tuned to make this benchmark pass.

Latency reports include successful transitions only, alongside missed completed
epochs and pending transitions. CUBIC successful detection mean is 297 ms
(n=16 runs), recovery 16.32 s (n=26); BBR detection 2.45 s (n=24), recovery
25.52 s (n=25). These are **not** unconditional response guarantees: the raw
reports record millions of unresolved pair-level epochs. Reference transitions
are sampled at 350 ms and can switch frequently as observed sharing changes.

Structural directed-queue sharing and active congestion are separate
references. Active reference requires utilization ≥0.8, backlog fraction ≥0.5
and mean sojourn ≥1 ms, plus observed shared traversal/residence. Evidence-time
and decision-time scores are both retained; route-transition windows are
excluded separately. The operational reference is not a universal causal
bottleneck oracle. Scaling its three thresholds jointly by 0.8 and 1.2 changes
conditional mean F1 to 0.0382/0.0080 for CUBIC and 0.3440/0.3548 for BBR.
Per-run profiles are archived; detector thresholds remain unchanged.

## Regression and external-validity limits

The existing moqlab unit suite produced **273 passed, 7 failed**. Each of the
same seven failures was reproduced against the user's already-staged baseline
in a separate scratch copy, without altering staged files. Failures:

- `test_config_schema::test_loads_linear_3r_1s_example`
- `test_designer::test_designer_schema_examples_validate_and_export_apis`
- `test_rtt::test_rtt_config_synthesis_and_container_mounts`
- `test_rtt::test_relay_rtt_supports_browser_clients`
- `test_rtt::test_rtt_graph_generated_after_network_stops`
- `test_switches::test_shared_example_separates_bottleneck_from_live_netem`
- `test_visualizer::test_browser_assets_live_outside_python_package`

Evidence remains in `.build/regression-tests-final.log` and
`.build/baseline-tests.log`. These pre-existing fixture/expectation failures
remain unresolved; a completely green shared repository suite is not claimed.

No controlled physical-network or Linux-QUIC calibration was performed.
Existing `moqlab/.runs/` media/emulation traces exist, but do not match this
picoquic, synthetic-byte and timestamp-feedback setup and were not pooled
as a validation reference. No Docker or Containernet experiment was launched.
Simulation-only RNG/certificates, ideal shared clock, custom timestamp stream,
synthetic workloads, ns-3 TCP, jitter distribution, wire overhead, queue models
and absence of OS/NIC scheduling are limits on external validity. Same-seed
semantic repeatability is checked, not universal byte-identical ciphertext.
For publication, report these assumptions and add a matched external reference
before claiming real-network equivalence.
