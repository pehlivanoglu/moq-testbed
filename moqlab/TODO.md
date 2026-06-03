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
- [ ] Decide whether Containernet link shaping should be symmetric or
      direction-aware. The current backend applies shaping to one side of each
      edge to avoid doubling one-way delay.
- [ ] Decide whether every derived edge must have an explicit `links:` entry
      for research reproducibility, or whether missing links may keep meaning
      "unshaped/default".
- [ ] First integration test under `tests/integration/` gated by
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
- [ ] TLS CA per run (`certs.py`); inject into every container; remove
      `tls.insecure: true` from production-style examples.
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
- [ ] Should publisher/subscriber definitions support a generic `flags: [...]`
      escape hatch for moxygen flags we have not first-classed? Useful for
      experiments; risk of becoming a junk drawer.
- [ ] If a future field requires fixed addressing beyond the backend's
      internal per-link subnets, decide whether to keep DNS-by-name or expose
      IPs from the schema. Strong preference: never expose IPs in the schema.
- [ ] Should browser subscribers be modeled as a first-class subscriber type,
      or added only when TLS and Playwright automation are ready?

## History Notes

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
  to the Containernet backend; `up` remains as a hidden compatibility alias.
- 2026-06-01: Added `python -m moqlab rm pycaches` to remove project
  `__pycache__` directories, bytecode files, and `.pytest_cache` while
  skipping `.venv` and `.runs`.
- 2026-06-01: Added `python -m moqlab doctor`, run readiness checks, and
  shared runtime helpers for relay ordering, topology edges, run ids, run
  dirs, and image tags.
- 2026-06-03: Added `python -m moqlab run --visualize` to serve a
  dependency-free localhost topology graph with live Containernet per-link
  throughput while the run is active.
