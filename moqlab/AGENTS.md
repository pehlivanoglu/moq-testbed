# AGENTS.md — `moqlab/` Python package

Component-level guide for AI agents touching the `moqlab` orchestrator. Read
this file completely before editing anything under `moqlab/`. The root
[../AGENTS.md](../AGENTS.md) sets project-wide invariants — those still apply;
this file adds component-specific rules.

## What's implemented

- **Schema** (`config/schema.py`) — Pydantic v2 model for the topology:
  `relays` + `publishers` + `subscribers` + optional `links`. Strict
  (`extra="forbid"`), unified node-id namespace, full cross-validation.
- **Synthesis** (`config/synth.py`) — turns the topology into per-relay
  moqx YAML files (mounted into relay containers) and argv lists for
  `moqdateserver` / `moqtextclient`.
- **Docker backend** (`orchestrator/docker_backend.py`) — bridge network +
  one container per node, container names = node ids = DNS labels.
- **Containernet backend** (`orchestrator/containernet_backend.py`) — same
  schema, attaches nodes via TCLinks through per-edge OVS bridges, explicitly
  launches relay/pub/sub binaries after `net.start()`, drops into `CLI(net)`.
- **CLI** (`cli.py`) — `up / down / validate / logs / ls` with
  `--backend [docker|containernet]`.
- 36 passing unit tests.

What is NOT implemented (tracked in [TODO.md](TODO.md)):

- Multiple upstreams per relay (mesh / fan-in).
- Generative topologies, scenario runner, observability, TLS CA per run,
  archiver, full `experiments/run_*/` layout, structured (JSONL) logging,
  Prometheus + Grafana sidecar.

## Layout

```
moqlab/
├── requirements.txt                ← runtime deps
├── requirements-dev.txt            ← + pytest
├── pytest.ini                      ← pytest config
├── conftest.py                     ← pytest rootdir marker (empty)
├── README.md                       ← user-facing quickstart + schema reference
├── AGENTS.md                       ← you are here
├── TODO.md                         ← deferred / future work
├── moqlab/                         ← package source (PEP 420 namespace package)
│   ├── __main__.py                 ← entry point for `python -m moqlab`
│   ├── cli.py
│   ├── exceptions.py
│   ├── config/
│   │   ├── schema.py
│   │   └── synth.py
│   └── orchestrator/
│       ├── docker_backend.py
│       └── containernet_backend.py
├── configs/examples/               ← example topologies (committed)
├── docker/                         ← Dockerfiles only; orchestration is in moqlab/
└── tests/unit/                     ← pytest, no Docker required
```

**moqlab is not a pip-installable library.** There is no `pyproject.toml`,
no `setup.py`, no console-script entry point. Users invoke it with
`python -m moqlab <subcommand>` from the project root. Runtime deps live in
`requirements.txt` and are installed into whatever venv is convenient (the
project's `.venv` for dev/docker work; the Containernet venv for the
Containernet backend). There is no `__init__.py` anywhere — the package is a
PEP 420 namespace package; `moqlab/__main__.py` is the entry point.

Use fully-qualified imports (`from moqlab.config.schema import …`); there
are no convenience re-exports.

## Hard rules for editing this package

1. **Config-driven, no CLI-arg configs, no hardcoded values.** Per root
   AGENTS.md IMPORTANT NOTE 3. The CLI takes a topology YAML; everything
   else (image, endpoint, ports, TLS, cache, pub/sub flags, link shaping)
   comes from that YAML.
2. **Pydantic v2 for every structured input.** Never round-trip a raw
   `dict`. New config fields go into `schema.py` with explicit types and
   validators. `extra="forbid"` is required so typos surface as
   `ConfigError`.
3. **No bare `Exception` / `RuntimeError`.** Use `moqlab.exceptions`:
   `ConfigError`, `OrchestratorError`, `RunNotFoundError`,
   `RunAlreadyExistsError`. Add new domain exceptions there if needed.
4. **No hardcoded IPs anywhere.** Both backends use container DNS — node id
   == container name == DNS label. Phase 2 link shaping uses TCLink, which
   does not require knowing IPs.
5. **State of truth lives in Docker labels** (Docker backend). Every
   container and network we create is tagged
   `moqlab.run_id=<run_id>` + `moqlab.role=<relay|publisher|subscriber>` +
   `moqlab.node_id=<id>`. `ls`, `down`, `container_for` re-derive state
   from Docker. The Containernet backend tears down at CLI exit; it does
   not persist state.
6. **No `print` in package code.** Use `click.echo` from CLI handlers and
   stdlib `logging` from library code (a later change moves library logging
   to `structlog`; see TODO).
7. **No internet access at runtime.** All container deps must be in the
   node images at build time.
8. **No `__init__.py`.** If you need package-level state, attach it to a
   submodule (e.g. `moqlab.config.schema._RELAY_ID_RE`), not a package
   `__init__`. The only module that lives at the package root is
   `moqlab/__main__.py`, and its sole responsibility is to invoke the
   Click `cli` object.
9. **Do not reintroduce `pyproject.toml`, `setup.py`, or any console-script
   entry point.** moqlab is run with `python -m moqlab`, not installed.
   New dependencies are added to `requirements.txt`.

## Where shared state lives

- **Docker labels** (`moqlab.run_id`, `moqlab.role`, `moqlab.node_id`) —
  authoritative for the Docker backend.
- **Run dir** at `<runs_dir>/<run_id>/`:
  - `topology.yaml` — exact copy of the input (traceability).
  - `configs/<relay_id>.yaml` — synthesized moqx config, bind-mounted into
    each relay container.
- **No `state.json` or pickle.** If a future feature needs more state, prefer
  Docker labels first; only if labels can't express it, add a YAML/JSON file
  with a Pydantic model for its schema.

## Adding a new schema field

1. Add it to the right Pydantic model in `moqlab/config/schema.py`. Use
   `_StrictBase` as the parent.
2. If it affects per-relay YAML or pub/sub argv, plumb it through
   `synth.py`.
3. Add unit tests in `tests/unit/test_config_schema.py` (valid + invalid)
   and `tests/unit/test_synth.py` (output shape).
4. Document it in `README.md` under the schema table.

## Adding a new backend

A new orchestrator backend (e.g. Kubernetes, AWS Fargate) goes at
`moqlab/orchestrator/<name>_backend.py`. Required invariants:

- Same `TopologyConfig` schema; no schema fork.
- Same per-relay YAML synthesis via `synth.synthesize_relay_configs` — do
  not duplicate.
- Idempotent teardown unless the UX is foreground (Containernet style).
- Same Docker labels if backend creates Docker containers.
- Wire CLI dispatch via the existing `--backend` choice list in `cli.py`.

## Tests

- `tests/unit/` is pytest-only, runs without Docker or Containernet, must
  pass on any laptop. Don't import `docker` or `mininet` at test-module
  level — keep these imports inside the modules under test.
- Integration tests (Docker + Containernet) are deferred. When they land
  they go in `tests/integration/` gated by an env flag, not run by default.

## Style

- Python 3.11+. `from __future__ import annotations` at the top of every
  module so we get cheap PEP 604 union types.
- Type-annotate every public function. Internal helpers may skip return
  annotations on trivial getters but should still annotate parameters.
- Module docstrings explain *why*, not *what*. No comments restating what
  the code does — comments are for non-obvious invariants or rationale.
- The orchestrator backends never import each other. `containernet_backend`
  may import only from `moqlab.config.*` and `moqlab.exceptions`.

## When to ask before changing

Per root AGENTS.md IMPORTANT NOTE 2, ask before:

- Adding any dependency not already in `requirements.txt`.
- Changing the schema in a non-additive way (any rename, removal, or
  meaning change of existing fields breaks topology configs in the wild).
- Adding new top-level CLI subcommands.
- Reintroducing `pyproject.toml` / `setup.py` / console-script entry points.
  The "run with `python -m moqlab`, never install" model is a deliberate
  choice — it avoids the cross-venv install ceremony that caused real pain
  when first wiring up the Containernet backend.
