# QUIC shared bottleneck simulation plan

Status: implemented and internally verified; see [VALIDATION.md](VALIDATION.md)
for evidence, detector coverage limitations and pre-existing regression failures.
Date: 2026-10-21.

## Objective and agreed scope

Evaluate server-side shared bottleneck detection using discrete-event network
simulation, without Docker, Containernet, Linux namespaces, or wall-clock network
I/O. Keep simulation implementation, dependencies, examples, tests, and generated
artifacts under `moqlab/simulation/`. Sharing existing topology validation,
designer assets, and dependency-light SBD sources is explicitly allowed.

- Primary research subject: SBD, not fidelity to the mvfst implementation.
- Real QUIC implementation: picoquic; congestion control: CUBIC and BBR.
  Record the exact implementation and BBR variant, rather than assuming
  equivalence to Linux or mvfst BBR. Additional controllers are deferred.
- Detector: existing `PracticalPassiveAndRFC`, preserving its PDV2/RFC behavior
  and reported algorithm name. DcSBD and Wei2020ECN are excluded.
- Detection compares eligible downstream connections at each server/relay.
- Sender sees receiver timestamps through modeled reverse-path feedback.
  Direct simulator observations are reserved for independent validation.
- Origins, forwarding relays, fan-out, clients, routers, transparent switches,
  asymmetric links, external traffic, and scheduled route changes are included.
- Designer edits simulation settings as well as physical topology.
- Target benchmark: 100 measured QUIC connections, 300 simulated seconds,
  30 independent repetitions per congestion controller on a reference topology.
  This is an acceptance workload to measure, not an existing speed guarantee.

## Architecture and boundaries

Use ns-3 for event scheduling, IP routing, network devices, TCP/UDP cross traffic,
and queue disciplines. Embed picoquic as an in-process QUIC library with its
packet input/output and wake-up interfaces driven exclusively by simulation time.
Use one simulation process per run; parallelize independent repetitions only.

The intended workflow, run from `moqlab/`, is:

```text
python -m simulation doctor
python -m simulation build
python -m simulation design -c simulation/examples/shared.yaml
python -m simulation validate -c simulation/examples/shared.yaml
python -m simulation run -c simulation/examples/shared.yaml
python -m simulation analyze <run-directory>
python -m simulation verify
```

These are proposed operational commands. Experiment parameters live in YAML.
The dedicated entry point must not import the existing `moqlab.cli`, whose
module imports the Docker backend and therefore the Docker Python package.
Simulation output goes to `moqlab/simulation/.runs/`; existing `.runs/` and
Containernet commands retain their current meanings.

Use existing Python Pydantic, PyYAML, and Click conventions. Use CMake for the
native simulation executable. Use installed plotting support where available;
make any extra plotting dependency explicit in simulation requirements. Pin
native dependency commits, transitive dependencies, patches, and archive hashes
in a lock manifest. No moving branch is used by a reproducible build.

Initial native candidates: a compatible ns-3 release, picoquic with its TLS
dependencies, and a tested external DualPI2 model. Stratum is a candidate for
DualPI2 because it advertises RFC conformance tests and a pinned ns-3 baseline;
its build requirements and patches must be evaluated before adoption. Do not
quietly replace DualPI2 with RED or a home-grown approximation if integration
fails. Exact pins are established by the first build/transport gate, before
building the rest of the system against them.

Expected implementation areas, with files split only as needed:

```text
simulation/
  __main__.py             dedicated CLI; no Docker import path
  schema.py               experiment document and validation
  runner.py               build, launch, batch execution, manifests
  designer.py             simulation document API using existing editor assets
  analysis.py             independent truth, scoring, plots
  CMakeLists.txt
  deps.lock.json
  native/                ns-3 scenario, picoquic adapter, apps, SBD adapter
  examples/              small validation cases and reference benchmark
  tests/                 Python, native, integration and designer checks
  README.md
  VALIDATION.md
  .build/                ignored generated artifacts
  .runs/                 ignored experiment outputs
```

## Configuration and designer

Use one versioned simulation YAML document containing `kind: simulation`,
`schema_version`, `topology`, and `experiment`. Reuse `TopologyConfig` inside
`topology`; keep simulation options out of the emulation topology schema.

The experiment model describes duration, independent run numbers/seeds, transport
and application defaults/overrides, explicit application paths where needed,
queues, feedback policy, timed events, trace level, and evaluation parameters.
All structured input uses strict Pydantic models. Validate finite numbers,
references, capacities, queue units, event order, routability, application
associations, supported options, and run-output paths before launch.

The existing designer gains a simulation mode backed by the separate simulation
API/schema. Reuse its canvas, inspector, schema forms, routes, import/export,
undo/redo, and keyboard accessibility. Add forms for QUIC workloads/controllers,
duration/seeds/repetitions, queue parameters, feedback, and timed route/link/AQM
events. Use mode-specific draft storage so switching workflows cannot overwrite
an emulation draft. All settings survive export/import and undo/redo. Users do
not need to hand-edit a second experiment file.

In simulation mode, display publisher as origin, relay as QUIC server/forwarder,
and subscriber as QUIC client. Existing topology YAML can be imported with a
visible conversion summary. Docker image, codec, media-track, and browser options
have no simulated effect and are listed as such. Reject unsupported behavior
requests such as active media caching instead of pretending to implement them.
Emulation cannot load the simulation wrapper as an ordinary topology.

Changes outside `simulation/` are limited to the shared designer server/assets
and their contract tests, any essential shared graph helper, and documentation.
Compile the existing SBD core sources directly; do not copy the algorithm or
link the full relay. A small shared extraction is allowed only if necessary to
preserve measurement semantics without pulling in mvfst/Folly; it must retain
existing relay behavior and pass existing regression tests.

## Network and transport semantics

- Directed egress serialization and capacity, then link propagation. AQM owns
  bottleneck backlog; hidden device queues cannot create an unreported second
  bottleneck. Log both queue-disc and device queues and account for wire bytes.
- FIFO, RED, PIE, FQ-CoDel, and DualPI2 are in scope. Record queue limits,
  thresholds, controller parameters, ECN mode, and source implementation.
  Validate DualPI2 classification, coupling, classic/scalable queues, overload,
  marking and dropping. ECT(1) alone is not evidence of L4S compliance; CUBIC/BBR
  runs are labeled according to their actual congestion response.
- Explicit asymmetric capacity/delay/loss. Jitter distribution, truncation,
  random streams, and whether jitter permits reordering are recorded. Do not
  imply exact Linux netem equivalence. Unspecified link rates and queue sizes
  resolve from visible simulation YAML defaults, never hidden infinity.
- Transparent switches use bridging rather than accidental IP forwarding.
  Preserve router egress queue ownership and named cross-traffic paths.
- Stable IP addresses and QUIC connections across explicit path changes.
  Forward and reverse routes are explicit. Apply a route update atomically at
  its event time; packets already in flight retain their scheduled traversal.
  Path changes are logged; detection follows the existing gap/reset policy
  unless an explicitly documented experiment policy overrides it.
- Link-state, rate, delay, loss, AQM and flow start/stop events are supported.
  Preserve queued packets for compatible parameter updates. Reject an AQM
  replacement with a nonempty queue unless the experiment explicitly chooses
  a logged drain or flush policy; never silently discard backlog.
- QUIC performs handshake, flow control, ACK generation, loss recovery,
  retransmission, pacing and congestion control. SBD uses transport-declared
  loss and spurious-loss corrections, not simulator queue-drop notifications.

## Applications and measurement

Origins generate synthetic bytes as saturated, paced, or segmented reliable
stream workloads. No video assets, codec, browser, HTTP, or MoQT are required.
Relays terminate upstream QUIC and fan out received data through separate
downstream connections. Define finite relay buffers and a lossless bounded
fan-out policy: a slow required child eventually backpressures upstream through
QUIC flow control. Record this coupling and test it; downstream data cannot be
generated before upstream delivery. Record offered application load separately
from transmitted QUIC rate. Support application pacing across segment boundaries
and byte accounting without rounding drift.

External traffic preserves bulk TCP, CBR UDP, and segmented TCP, and adds QUIC
bulk. Multiple senders/receivers and per-flow paths are supported. CBR timing uses
simulation events; TCP uses the documented ns-3 stack, not assumed Linux TCP.

For packet-level OWD, the receiver records QUIC application-data packet number
and receive timestamp, and returns cumulative, deduplicated feedback over a
dedicated reliable stream on the same connection. The sender joins this with
its own send ledger only after feedback arrives. Feedback batching, framing,
bytes, timers, loss/retransmission, and flow-control effects are explicit and
measured. Instrument at decrypted QUIC packet boundaries, not application
stream callbacks or UDP datagram boundaries; handle coalesced packets.
Use minimal pinned picoquic instrumentation patches if required and include
their tests and hashes. Preserve the distinction from mvfst ACK timestamp
extensions and quantify resulting observation-delay differences.

Use one ideal shared simulation clock for the baseline, explicitly reporting
this assumption. Keep detector input separate from queue and route truth.
Preserve 350 ms receiver intervals, 50-interval estimator warm-up, 100-interval
grouping warm-up, cumulative receiver watermarks, send-time loss buckets,
staleness, empty-interval reset/rewarm, and latest common completed interval.
Measure decision time independently from the receiver evidence interval.
Changing the timing to virtual time must not change the estimator equations.

## Ground truth and scoring

Record independent packet queue residence, enqueue/dequeue/drop/mark events,
actual paths, capacity history, flow identity, and application/transport state.
Ground truth never feeds the detector.

Publish two references: structural directed-queue sharing and active congestion
sharing. Define active congestion by a documented observation window and
explicit utilization/backlog/sojourn criteria, with sensitivity analysis; queue
sharing or one busy instant alone is not a universal bottleneck oracle. Preserve
the raw evidence so alternative definitions can be applied offline. Route events
make the reference time dependent. Score overlapping relationships pairwise.

Report TP/FP/TN/FN, precision, recall, F1, false-positive rate, detection and
recovery delay, coverage, unknown/stale/warm-up time, and per-run distributions.
Unknown decisions must not become negative predictions. Align evidence-based
scores to receiver intervals, and separately report operational scores at
decision time so delayed feedback cannot use future truth. Handle undefined
ratios explicitly. Compute confidence intervals over independent repetitions,
not correlated time samples. Use a fixed, recorded analysis seed.

Separate code/model validation from scientific detector performance. A validated
simulation may expose poor SBD accuracy; tests must not tune workload or
thresholds until the detector appears successful. Published claims state the
QUIC, application, clock, routing, and AQM assumptions.

## Verification gates and execution sequence

1. Build/transport gate: lock compatible dependencies; run upstream tests;
   build a two-node QUIC transfer; prove virtual-clock wakeups, CUBIC and the
   selected BBR variant, ECN propagation, packet instrumentation, and real
   reverse-path feedback. Check all randomness that affects timing, including
   QUIC random state; do not assume the ns-3 seed controls crypto randomness.
   Repeat semantic traces with identical configuration. If identical packet
   timing is not achievable, expose and explain the remaining nondeterminism
   instead of promising deterministic simulation.
2. Network gate: analytic serialization/propagation and byte conservation;
   asymmetric links, reverse ACK congestion, loss/jitter streams, bridging,
   every AQM, queue ownership, route changes and in-flight packet behavior.
   Carry reference vectors and numerical tolerances in tests.
3. Measurement/SBD gate: existing core fixtures and an independent reference
   calculation for summaries; packet trace replay through old/new measurement
   adapters; interval/group equivalence, feedback loss/reordering, staleness,
   reset, transport loss/spurious correction, coalescing, and connection close.
   A test-only oracle may compare true receive times, never supply online data.
4. Application gate: fan-out byte identity and upstream dependency, bounded
   buffering/backpressure, saturation/paced/segment timing, external traffic
   accounting, multiple routes and simultaneous event ordering.
5. End-to-end cases: uncongested shared path, active shared bottleneck,
   independent bottlenecks, shared then private/tandem bottlenecks, correlated
   independent cross traffic, overlapping sharing, reverse-path congestion,
   flow churn, capacity changes, path changes, and feedback impairment.
   Exercise CUBIC/BBR and queue variants using a documented coverage matrix.
6. Designer/regression gate: schema reachability, import/export and undo/redo,
   mode isolation, all simulation event controls, legacy topology examples,
   existing moqlab unit and designer tests. Prove simulation CLI/run works
   without Docker/Containernet imports or privileged network setup.
7. Research/scale gate: run the 100-connection, 300-second reference case for
   30 independent repetitions per controller; report wall time, peak RSS,
   output volume, variability and 95% confidence intervals. Benchmark detailed
   tracing separately and provide trace levels to control output volume.
   Do not claim scalability until the workload has actually completed.
8. Validation report: independent reference comparison where possible; reuse
   available emulation traces or a small non-container real-QUIC reference,
   without introducing a runtime dependency on emulation. Record absent
   physical/Linux reference evidence as an external-validity limit. No full
   real-network equivalence claim follows from internal tests alone.

Gates are internal implementation checkpoints within the approved delivery, not
requests for repeated user approval. An unavailable or failing required model
is a material blocker to report; unsupported behavior is not quietly omitted.

## Artifacts and acceptance

Each run archives original/resolved YAML, git commit plus dirty-source hashes,
dependency and patch revisions, build options, seed/stream assignments, model
parameters, ordered event log, command, simulation completion status, metrics,
SBD summaries/groups, independent truth, and analysis metadata. Failures and
partial output remain identifiable; successful completion is marked only after
output flush and accounting checks. Existing output directories are not
overwritten. JSONL/CSV summaries are standard; PCAP/qlog and detailed packet
traces are selectable and are enabled for diagnostic verification cases.

Delivery is accepted when all agreed features work through designer and CLI,
required verification gates have evidence, regression tests pass, and the
validation report distinguishes passed checks, observed SBD weaknesses,
remaining nondeterminism and external-validity limits. Installation/download
requires network access; already-built experiments do not.

Out of scope: MoQT/media playback, cache semantics, DcSBD/Wei algorithms,
additional congestion controllers, Wi-Fi/cellular/mobility, HTTP/3, multipath
QUIC, OS scheduler/NIC-offload fidelity, and a separate hosted web application.
Route changes use single-path IP routing and do not imply multipath QUIC.

## Sources consulted

- Existing `moqlab/AGENTS.md`, `TODO.md`, topology/designer/traffic code,
  `src/sbd/` and SBD tests/implementation notes.
- Picoquic packet API and virtual-clock interface:
  https://github.com/private-octopus/picoquic/blob/master/picoquic/picoquic.h
- Report of picoquic/ns-3 integration (not a validation of this project):
  https://datatracker.ietf.org/meeting/126/materials/minutes-126-ccwg-02
- DualPI2 candidate and its declared tests/build requirements:
  https://apps.nsnam.org/app/stratum/
  https://github.com/digitalities/stratum-ns3

Planning preceded implementation. Completed build/runtime evidence and the
remaining external-calibration limit are recorded in [VALIDATION.md](VALIDATION.md).
