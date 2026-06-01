# AGENTS.md - `moqlab/` Multirelay Testbed

This is the authoritative guide for AI agents working on `moqlab/`.
There is intentionally no root-level agent guide right now: keep agent-authored
instructions, plans, and moqlab-specific notes inside this directory.

Read this file and [TODO.md](TODO.md) before editing anything under `moqlab/`.
After any change, update the relevant Markdown in this directory so deferred
work, missing features, and behavior changes stay visible.

## Project Identity

`moqlab` is a PhD research testbed for Media over QUIC (MoQT) multirelay
experiments. It lets a researcher define CDN-like relay topologies in YAML,
instantiate them as Docker containers, optionally connect them through
Containernet/Mininet with shaped links, and eventually run repeatable
network-impairment scenarios with structured logs and QUIC traces.

Optimize every decision for correctness, reproducibility, and researcher
ergonomics. This is research infrastructure, not a demo script.

## Collaboration Rules

- Discuss design choices before making them. Do not make schema, architecture,
  dependency, or workflow decisions silently.
- Do not add new hardcoded behavior or new CLI configuration knobs. The
  topology YAML should be the source of truth for images, ports, endpoints,
  TLS, cache, launch timing, pub/sub behavior, and link shaping.
- Existing CLI options that are operational rather than topology-defining are
  tolerated for now; if a new need is really experiment configuration, put it
  in the config schema and document it.
- Do not introduce a dependency without discussion. If accepted, add it to
  `requirements.txt` with a pinned range and document why.
- Do not create new project-control documents outside `moqlab/` unless the
  researcher explicitly asks for root-level files again.

## Current Implementation

- **Schema** (`moqlab/config/schema.py`) - Pydantic v2 topology model:
  `defaults`, `startup`, `relays`, `publishers`, `subscribers`, and optional
  `links`. It is strict (`extra="forbid"`) and validates node ids, relay
  references, port collisions, cycles, and duplicate links.
- **Synthesis** (`moqlab/config/synth.py`) - turns topology config into
  per-relay moqx YAML files and argv lists for `moqdateserver` and
  `moqtextclient`.
- **Docker backend** (`moqlab/orchestrator/docker_backend.py`) - creates one
  bridge network and one detached container per node. Docker labels are the
  state of truth.
- **Containernet backend** (`moqlab/orchestrator/containernet_backend.py`) -
  creates Docker hosts inside Containernet, attaches endpoints through
  per-edge OVS bridges with `TCLink`, starts node binaries explicitly after
  `net.start()`, opens `CLI(net)`, and tears down on CLI exit.
- **CLI** (`moqlab/cli.py`) - `build moqx`, `build images`, `validate`,
  `run`, `down`, `logs`, `ls`, and `rm pycache`. `up` remains a hidden
  compatibility alias for `run`.
- **Tests** (`tests/unit/`) - unit tests for schema, synthesis, and
  Containernet launch ordering. They do not require Docker, Containernet, or
  root.

Not implemented yet: multiple upstreams per relay, generative topologies,
scenario runner, observability, JSONL collector, Prometheus/Grafana, QLOG
archive, TLS CA per run, full `experiments/run_*/` archive layout, replay,
and analysis helpers. Track all of these in [TODO.md](TODO.md).

## Current Layout

```
moqlab/
├── AGENTS.md
├── README.md
├── TODO.md
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── conftest.py
├── configs/
│   └── examples/
├── docker/
│   ├── Dockerfile.relay
│   ├── Dockerfile.pub
│   ├── Dockerfile.sub
│   └── README.md
├── moqlab/
│   ├── __main__.py
│   ├── build.py
│   ├── cli.py
│   ├── exceptions.py
│   ├── config/
│   │   ├── schema.py
│   │   └── synth.py
│   └── orchestrator/
│       ├── docker_backend.py
│       └── containernet_backend.py
└── tests/
    └── unit/
```

`moqlab` is not a pip-installable library right now. There is no
`pyproject.toml`, no `setup.py`, no console script, and no package
`__init__.py`. Run it from this directory with `python -m moqlab`.

Use fully qualified imports such as `from moqlab.config.schema import ...`.
There are no convenience re-exports.

## Current Config Shape

The implemented schema is explicit-only:

```yaml
topology_mode: explicit

defaults:
  relay:
    image: moqlab-relay
    endpoint: /moq-relay
    tls: { insecure: true }
    cache: { enabled: false, max_tracks: 100, max_groups_per_track: 3 }
  publisher:
    image: moqlab-pub
    insecure: true
    log_level: INFO
  subscriber:
    image: moqlab-sub
    insecure: true
    log_level: INFO

startup:
  relay_warmup_s: 2.0
  publisher_warmup_s: 1.0

relays:
  relay-a: { listen_port: 9668, admin_port: 9669, upstream: null }
  relay-b: { listen_port: 9670, admin_port: 9671, upstream: relay-a }

publishers:
  pub: { connects_to: relay-a, namespace: moq-date }

subscribers:
  sub: { connects_to: relay-b, namespace: moq-date, track: date }

links:
  - { from: pub, to: relay-a, bandwidth_mbps: 100, delay_ms: 5 }
  - { from: relay-a, to: relay-b, bandwidth_mbps: 50, delay_ms: 20 }
  - { from: relay-b, to: sub, bandwidth_mbps: 20, delay_ms: 69 }
```

Implemented invariants:

- `topology_mode` must be `explicit`.
- Node ids are globally unique and valid as Docker names/DNS labels.
- Relay `upstream` and pub/sub `connects_to` references must target known
  relays.
- A relay has at most one upstream; upstream chains must be cycle-free.
- Relay listen/admin ports must be unique across the topology.
- `startup` warmups are non-negative seconds and config-driven.
- `links` reference known nodes and may not duplicate an undirected pair.
- Unknown fields are rejected.

Future target config work may introduce named `networks`, relay `upstreams`,
ECN/L4S fields, browser subscribers, scenarios, and generative topology mode.
Do not start that migration without discussion.

## Containernet Notes

Containernet is Mininet extended with Docker-container hosts. The important
mental model is:

```text
Host machine
└── Containernet process
    ├── Docker host: relay/pub/sub container
    ├── Docker host: relay/pub/sub container
    └── OVS switches and TCLinks connecting those hosts
```

Applications run inside the Docker hosts, not on the Ubuntu host running
Containernet. Do not use host `localhost` for node-to-node traffic. In the
current backend, nodes should address each other by node id/DNS label, while
the backend manages per-link addresses internally.

Containernet specifics that matter in this repo:

- Use `addDocker(...)` for node containers.
- Use `TCLink` for link shaping.
- `net.start()` starts the Mininet network, not the moqx/moxygen binaries.
  The backend must launch relays, publishers, and subscribers explicitly after
  `net.start()`.
- Keep Containernet imports lazy so Docker-only workflows still work on normal
  developer machines.
- Containernet runs foreground and tears down when `CLI(net)` exits.

## State And Runtime Outputs

- Docker backend state lives in Docker labels:
  `moqlab.run_id`, `moqlab.role`, and `moqlab.node_id`.
- Current run scratch data lives under `<runs_dir>/<run_id>/`:
  `topology.yaml` plus synthesized `configs/<relay_id>.yaml`.
- The current default is `moqlab/.runs/`; the future experiment archive may
  move to `<repo_root>/experiments/run_*`. Decide and document that before
  implementing the archive.
- Do not commit runtime output, `.runs/`, `.venv/`, `.pytest_cache/`, or
  `__pycache__/`.
- Use `python -m moqlab rm pycache` to remove project bytecode caches and
  `.pytest_cache` when they clutter the tree. It intentionally skips `.venv`
  and `.runs`.

## Coding Rules

- Python 3.11+.
- Put `from __future__ import annotations` at the top of every Python module.
- Use Pydantic v2 for every structured input. Never keep structured config as
  raw dictionaries after loading.
- Use domain exceptions from `moqlab/exceptions.py`; add new domain exceptions
  there if needed.
- Do not add `print()` in package code. CLI handlers use `click.echo`; library
  code currently uses stdlib `logging` until the tracked `structlog` migration.
- Type annotate public function signatures. Avoid `Any` unless the shape is
  genuinely external or dynamic; keep it localized and obvious.
- Keep backend modules independent. They may share helpers, but they should
  not import each other.
- Prefer config/schema helpers over duplicating string construction in
  backends.
- Do not add comments or docstrings that merely restate a function, class, or
  file name. For example, avoid docstrings like "Commands that build the local
  Docker images" on a function already named `docker_image_build_commands`.
  Keep comments only when they explain non-obvious rationale, external quirks,
  constraints, or surprising behavior.
- Click command help should live in Click decorators when the function name is
  already self-explanatory. Do not keep trivial function docstrings only for
  command help text.

## Docker Rules

- Dockerfiles are image-build inputs only; orchestration belongs in Python.
- Build preparation is separate from running topologies:
  `python -m moqlab build moqx` prepares binaries and
  `python -m moqlab build images` builds local node images.
- Images must not contain secrets. TLS material will eventually be injected at
  runtime from the run directory.
- Image defaults are local-development defaults; topology config can override
  image names per role or per node.
- Future production-style Dockerfiles should pin base images by full digest
  and use multi-stage builds when binaries are built in image.

## Scenario And Observability Target

Scenarios will be separate YAML files applied to a running topology. Time
values should be strings such as `30s`, `2m`, and `90s`, parsed by scenario
code with monotonic timing. Planned actions include:

- `set_link`
- `add_loss`
- `restore`
- `add_node`
- `remove_node`
- `set_ecn`
- `set_l4s`

Every experiment run should eventually be reproducible from its run directory,
including exact topology/scenario copies, resolved generated topology, git
hashes, host info, image digests, logs, qlog, metrics, netem command log, and
a final `SEALED` sentinel for completed runs.

Structured node logs should be JSONL on stdout with required fields:
`ts`, `run_id`, `node`, `node_type`, `event`, and `level`.
Never log raw binary data or TLS key material.

## Adding A Schema Field

1. Add it to the right Pydantic model in `moqlab/config/schema.py`.
2. Validate it with explicit types and `model_validator` if needed.
3. Plumb it through `synth.py` if it affects relay YAML or pub/sub argv.
4. Add focused tests in `tests/unit/test_config_schema.py` and, when relevant,
   `tests/unit/test_synth.py`.
5. Document it in `README.md`.

## Adding A Backend

New backends go in `moqlab/orchestrator/<name>_backend.py`.

Required invariants:

- Use the same `TopologyConfig`; do not fork the schema.
- Reuse synthesis helpers instead of duplicating relay YAML or argv logic.
- Make teardown idempotent unless the backend is intentionally foreground like
  Containernet.
- Use the same Docker labels if the backend creates Docker containers.
- Wire CLI dispatch through the existing backend choice only after discussion.

## Tests

- Unit tests live in `tests/unit/`; they must not require Docker,
  Containernet, root, or network access.
- Do not import `docker`, `mininet`, or `containernet` at test-module import
  time unless the test is explicitly gated as integration-only.
- Integration tests are deferred. When added, put them under
  `tests/integration/` and gate them with an environment variable such as
  `MOQLAB_INTEGRATION=1`.

## When To Ask First

Ask before:

- Adding or changing dependencies.
- Changing the config schema in a non-additive way.
- Adding top-level CLI commands or new CLI configuration flags.
- Reintroducing `pyproject.toml`, `setup.py`, console scripts, or package
  `__init__.py` files.
- Moving run output from `.runs/` to an experiment archive layout.
- Implementing intentionally incomplete behavior. If something is deferred,
  document it in [TODO.md](TODO.md).
