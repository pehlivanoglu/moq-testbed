# TODO - `moqlab/` Package

Deferred work for the `moqlab` orchestrator. Items here are scope explicitly
cut from current versions, not bugs or oversights.

## Documentation And Hygiene

- [ ] Keep all moqlab-specific agent guidance and plans under `moqlab/`.
      The old root `AGENTS.md`, `TODO.md`, and `containernet.md` were removed
      so this folder is the single home for the testbed instructions.
- [ ] Keep `README.md`, `AGENTS.md`, `TODO.md`, and `docker/README.md` aligned
      whenever behavior, layout, commands, or config schema change.
- [ ] Review the root repository docs that still mention general moqx config
      behavior (`README.md`, `RUNNING.md`, `docs/config.md`) separately from
      moqlab so moqx and moqlab wording do not drift into each other.
- [ ] Decide whether legacy root relay fixtures (`relay-a.yaml`,
      `relay-b.yaml`, `relay-c.yaml`, if present again) should remain outside
      moqlab. They are not referenced by current moqlab code.

## Now-Shippable Cleanups

- [x] Fix or clarify Docker image build commands and context in `README.md`
      and `docker/README.md`; `moqlab build images` now builds from the
      repository root context.
- [ ] Pin Dockerfile base images by full digest or explicitly document that
      the current floating `ubuntu:24.04` images are local-development only.
- [ ] Move experiment-relevant CLI knobs such as readiness timing into the
      topology config if they should be reproducible config, not operator
      convenience.
- [ ] Finish factoring shared launch logic so publisher warmup and
      subscriber launch order live in one place. Relay ordering, topology
      edges, run ids, run dirs, and image tag collection are already shared.
- [x] Decide whether Containernet link shaping should be symmetric or
      direction-aware. Resolved 2026-06-12: links carry per-direction
      `forward:`/`reverse:` blocks; each compiles to an egress qdisc chain on
      the owning interface.
- [x] Decide when to introduce explicit router/link-emulator nodes for ECN,
      L4S, AQM, and underlay routing experiments. Resolved 2026-06-12:
      `routers:` are first-class Docker hosts; see `ROUTER.md`.
- [x] Decide whether every derived edge must have an explicit `links:` entry.
      Resolved 2026-06-12: `links:` is the physical wiring source of truth on
      the Containernet backend (which refuses configs without it); the schema
      verifies every app edge has a path through the link graph.
- [ ] Add the first text integration test under `tests/integration/` gated by
      `MOQLAB_INTEGRATION=1`: bring up `linear_3r_1s.yaml` on the Docker
      backend, assert every container reaches `running`, assert the subscriber
      receives at least one date object, then `down`.
- [ ] `moqlab status --run-id NAME` reporting per-node container status,
      recent log tail, and admin-port reachability.
- [ ] Extend the first localhost visualizer beyond Containernet interface
      counters once a real observability collector exists. The current
      `moqlab run --visualize` flag intentionally avoids guessing Docker
      per-link throughput from aggregate bridge counters.
- [ ] Decide whether `moqlab run` should grow a plan/preview mode later, and
      what exact output should be useful for experiments.
- [ ] Decide on run-dir location (`moqlab/.runs/` vs
      `<repo_root>/experiments/run_*`) once the experiment-archive layout
      lands. Document the choice in `README.md`.

## Phase 4 - Mesh / Fan-In

- [ ] Allow `upstream: [<id>, <id>, ...]` or
      `upstreams: { <id>: <link_name> }`.
- [ ] Synthesize one moqx `services` block per upstream with distinct match
      rules, likely by authority and/or path.
- [ ] Generalize cycle detection from the single-upstream walk to a proper
      DAG check.
- [ ] Add unit tests for multi-upstream YAML synthesis and validation.

## Phase 5 - Generative Topologies

- [ ] Allow `topology_mode: generative` (currently rejected).
- [ ] Add `config/generator.py` with seed-driven random construction:
      linear, tree, mesh, and random.
- [ ] Seed all relevant RNGs before generation.
- [ ] Write the resolved explicit graph to `topology_resolved.yaml` before
      starting containers.

## Phase 6 - Scenarios, Observability, And Experiment Archive

These become tractable once Phase 4 lands.

- [ ] `scenario/` package: time-sequenced events, `netem` runner, ECN/L4S
      kernel probes, and monotonic scenario timing.
- [ ] Scenario actions: `set_link`, `add_loss`, `restore`, `add_node`,
      `remove_node`, `set_ecn`, and `set_l4s`.
- [ ] `observability/` package: JSONL collector, Prometheus/Grafana sidecar,
      and QLOG copy at end of run.
- [ ] Full `experiments/run_*/` layout with `RUN_ID`, `config.yaml`,
      optional `scenario.yaml`, optional `topology_resolved.yaml`, `git.json`,
      `host_info.json`, `logs/`, `qlog/`, `metrics/`, `netem.log`, and
      `SEALED`.
- [ ] Abort publishable runs if moqlab/moqx/moxygen git state is dirty, with
      an explicit development override only after discussion.
- [ ] Record full Docker image digests in run metadata.
- [ ] Replace the implemented shared per-run self-signed media certificate
      with a CA/leaf hierarchy if production-style trust becomes necessary.
- [ ] Extend `moqlab run` into a full experiment command that can combine
      topology startup, scenario execution, archiving, and teardown.
- [ ] Replay (`moqlab replay`) and post-experiment analysis
      (`analysis/metrics.py`).
- [ ] Replace stdlib `logging` with `structlog` and bind `run_id` at run
      start.
- [ ] `Makefile` with `images`, `test-unit`, `test-integration`, `lint`, and
      `clean-runs` targets if we decide Make is still wanted in the no-install
      workflow.

## Open Questions

- [ ] Should the schema gain a top-level `version:` field so it can evolve
      with explicit migrations? Decide before Phase 4.
- [ ] Should the future config use named `networks:` blocks instead of the
      current edge-list `links:` shape? Named networks may make scenario files
      much cleaner.
- [x] Should ECN/L4S/AQM scenario support require a router/link-emulator
      backend mode? Resolved 2026-06-12: AQMs only attach on router egress
      (router image carries the modern tc); endpoint-side netem/HTB remains
      available. See `ROUTER.md`.
- [ ] Should publisher/subscriber definitions support a generic `flags: [...]`
      escape hatch for moxygen flags we have not first-classed? Useful for
      experiments; risk of becoming a junk drawer.
- [ ] If a future field requires fixed addressing beyond the backend's
      internal per-link subnets, decide whether to keep DNS-by-name or expose
      IPs from the schema. Strong preference: never expose IPs in the schema.
- [x] Browser subscribers are first-class `kind: media` nodes using Chromium
      CDP automation and generated per-run TLS; no Playwright dependency.

## History Notes

- 2026-08-03: Added clear LOC AV1 spatial-SVC media nodes, generated shared
  TLS, local-context image builds, headless/headed Chromium readiness, noVNC,
  and a gated Docker acceptance test.

- 2026-08-04: Added direct host-X11 Chromium mode for local Containernet and
  Docker inspection without Xvfb/noVNC.

- 2026-05-28: Landed first `moqlab/` Python package slice with topology
  schema/synthesis and Docker backend.
- 2026-05-29: Added publishers/subscribers, optional `links:`, and the
  Containernet backend.
- 2026-05-29: Removed install/package metadata; `moqlab` is invoked with
  `python -m moqlab` from this directory.
- 2026-05-29: Fixed Containernet launch sequencing by explicitly starting
  moqx/moxygen binaries after `net.start()`.
- 2026-05-29: Added config-driven `startup:` warmups for relay and publisher
  launch timing.
- 2026-06-01: Consolidated root `AGENTS.md`, root `TODO.md`, and
  `containernet.md` into `moqlab/AGENTS.md` and this TODO. Removed those root
  files so moqlab-specific guidance lives inside `moqlab/`.
- 2026-06-01: Added `python -m moqlab build moqx`,
  `python -m moqlab build images`, and `python -m moqlab run`. `run` defaults
  to the Containernet backend.
- 2026-06-01: Added `python -m moqlab rm pycaches` to remove project
  `__pycache__` directories, bytecode files, and `.pytest_cache` while
  skipping `.venv` and `.runs`.
- 2026-06-01: Added `python -m moqlab doctor`, run readiness checks, and
  shared runtime helpers for relay ordering, topology edges, run ids, run
  dirs, and image tags.
- 2026-06-03: Added `python -m moqlab run --visualize` to serve a
  dependency-free localhost topology graph with live Containernet per-link
  throughput while the run is active.
- 2026-06-03: Moved visualizer browser code out of Python and into
  `moqlab/visualizer/{index.html,style.css,app.js}`.
- 2026-06-03: Added mouse-driven pan and zoom to the localhost topology graph.
- 2026-06-03: Suppressed harmless visualizer `BrokenPipeError` tracebacks when
  browser polling requests disconnect before the response is written.
- 2026-06-03: Moved the visualizer links table into a fixed right-side panel
  so the main page does not need to scroll on desktop.
- 2026-06-03: Wrapped Containernet `TCLink` setup to tune HTB `r2q` and avoid
  issuing invalid handle-zero qdisc deletes while preserving real tc failures
  in the console.
- 2026-06-12: Replaced per-edge OVS switches + `TCLink` with explicit router
  containers and direct veth links. Hard-broke the `links:` schema to
  per-direction `forward:`/`reverse:` shaping with `aqm: dualpi2` support,
  added per-node /32 loopbacks + static routes, a dedicated `moqlab-router`
  image (pinned iproute2 build), and pure `shaping.py`/`routing.py` modules.
  The Docker backend now refuses router topologies.
