# TODO - moqx Root

Root-level follow-ups and repository-wide notes for AI agents.

## Documentation

- [ ] Add component-level `AGENTS.md` and `TODO.md` files as components are
  touched in future prompts.
- [ ] Keep root known limitations in `AGENTS.md` aligned with implementation
  and public docs.
- [ ] Review `docs/config.md` against current code for draft/version wording,
  especially draft 15 support and released-vs-current config fields.

## Project Hygiene

- [ ] Decide whether staged root documentation files should be committed with
  the existing staged `.gitmodules`, `deps/moxygen`, and draft text changes.

## Notes

- 2026-05-24: Replaced the initial moqlab-oriented root `AGENTS.md` draft with
  moqx-specific guidance and added the component documentation workflow.
- 2026-05-25: Added relay object-arrival logging in `src/relay/TopNFilter.cpp`
  so relay flow can be observed from object ingress logs.
- 2026-05-28: Landed `moqlab/` Python package v1 — topology generator + Docker
  backend (relays only, single upstream per relay). New entry point: `moqlab`
  CLI with `up / down / validate / logs / ls`. Example config at
  `moqlab/configs/examples/linear_3relay.yaml` reproduces the C→B→A chain
  expressed today by the hand-written `relay-a.yaml` / `relay-b.yaml` /
  `relay-c.yaml`. Containernet backend, pubs/subs, mesh, scenarios, and
  observability are explicitly deferred — tracked in `moqlab/TODO.md`.
- 2026-05-29: Extended `moqlab/` to v0.2.0 — added publishers/subscribers and
  optional `links:` block to the schema, plus a Containernet backend behind
  `moqlab up --backend [docker|containernet]`. Removed legacy hand-written
  `moqlab/containernet/topology.py` and `moqlab/docker/docker-compose.yml`
  (replaced by the orchestrator). Switched the package to a PEP 420 namespace
  layout (no `__init__.py`) for repo tidiness. The root `relay-a/b/c.yaml`
  fixtures are no longer referenced by any moqlab code and can be removed
  when convenient. 36 unit tests; mesh / generative / scenarios / observability
  still deferred per `moqlab/TODO.md`.
- 2026-05-29 (later): Removed `moqlab/pyproject.toml` and the `moqlab` console
  script. moqlab is now invoked as `python -m moqlab` from the project dir.
  Runtime deps moved to `requirements.txt`; dev deps to `requirements-dev.txt`;
  pytest configured via `pytest.ini` + an empty `conftest.py`. Rationale: the
  cross-venv `pip install -e` ceremony for the Containernet backend was
  brittle and conflicted with an unrelated `moqlab` package on PyPI. With no
  install, you just install the deps once into whatever venv you want to use
  and run `python -m moqlab` from `moq-testbed/moqlab/`.
- 2026-05-29 (later): Fixed the Containernet backend launch sequence. Mininet's
  `net.start()` starts controllers/switches, not Docker host ENTRYPOINTs, so
  the backend now explicitly starts moqx, waits briefly for relay listeners,
  then starts the publisher and subscriber binaries. Added a unit test for
  launch order and command shape.
- 2026-05-29 (later): Added a config-driven `startup:` block to moqlab
  topologies (`relay_warmup_s`, `publisher_warmup_s`) and wired both Docker
  and Containernet launch order through it. This avoids subscriber
  `no such namespace or track` races without CLI timing flags.
