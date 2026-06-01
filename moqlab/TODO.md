# TODO — `moqlab/` package

Deferred work for the `moqlab` orchestrator. Items here are scope explicitly
cut from current versions, not bugs or oversights.

## Now-shippable cleanups

- [ ] First integration test under `tests/integration/` gated by
      `MOQLAB_INTEGRATION=1`: brings up `linear_3relay.yaml` on the Docker
      backend, asserts every container reaches `running`, asserts the sub
      receives at least one date object, then `down`.
- [ ] `moqlab status --run-id NAME` reporting per-node container status,
      recent log tail, and admin-port reachability.
- [ ] `--dry-run` for `moqlab up` that synthesizes the run dir and prints
      what would be launched but skips Docker/Containernet calls.
- [ ] Decide on run-dir location (`moqlab/.runs/` vs
      `<repo_root>/experiments/run_*`) once the experiment-archive layout
      lands. Document the choice in `README.md`.

## Phase 4 — Mesh / fan-in (multiple upstreams per relay)

- [ ] Allow `upstream: [<id>, <id>, …]` or
      `upstreams: { <id>: <link_name> }`.
- [ ] Synthesize one moqx `services` block per upstream with distinct match
      rules (authority / path).
- [ ] Generalize cycle detection from the single-upstream walk to a proper
      DAG check.
- [ ] Add unit tests for multi-upstream YAML synthesis and validation.

## Phase 5 — Generative topologies

- [ ] Allow `topology_mode: generative` (currently rejected).
- [ ] Add `config/generator.py` with seed-driven random construction
      (linear, tree, mesh, random).
- [ ] Write the resolved explicit graph to `topology_resolved.yaml` before
      starting containers (reproducibility contract from root AGENTS.md).

## Phase 6 — Scenarios + observability + experiment archive

These become tractable once Phase 4 lands.

- [ ] `scenario/` package — time-sequenced events, `netem` runner, ECN/L4S
      kernel probes. Per root AGENTS.md `Scenario format` section.
- [ ] `observability/` — JSONL collector, Prometheus + Grafana sidecar,
      QLOG copy at end of run.
- [ ] Full `experiments/run_*/` layout with `SEALED` sentinel and
      `host_info.json` / `git.json`.
- [ ] TLS CA per run (`certs.py`); inject into every container; remove
      `tls.insecure: true` from production-style examples.
- [ ] `moqlab run` super-command that combines `up`, scenario execution,
      and `down` in one shot.
- [ ] Replay (`moqlab replay`) and post-experiment analysis
      (`analysis/metrics.py`).
- [ ] Replace stdlib `logging` with `structlog` and bind `run_id` at run
      start.
- [ ] `Makefile` with `images / test-unit / test-integration / lint /
      clean-runs` targets per root AGENTS.md `Commands` section.

## Open questions to revisit

- Should the schema gain a top-level `version:` field so we can evolve it
  with explicit migrations? Decide before Phase 4.
- Should publisher/subscriber definitions support a generic `flags: [...]`
  escape hatch for moxygen flags we haven't first-classed? Useful for
  experiments; risk of becoming a junk drawer.
- If a future field requires fixed addressing beyond the backend's internal
  per-link subnets, decide whether to keep DNS-by-name or expose IPs from
  the schema. Strong preference: never expose IPs in the schema.
