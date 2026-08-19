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
  `defaults`, `startup`, `relays`, `publishers`, `subscribers`, `routers`, `traffic`,
  and `links` (per-direction `forward`/`reverse` shaping incl. `aqm`). Pub/sub
  nodes are media-only and model `mlmpub`, Chromium WARP Player, and native
  `mlmsub` subscribers. It is
  strict (`extra="forbid"`) and validates node ids, relay references, port
  collisions, cycles, duplicate links, link-graph reachability of every
  app edge, aqm-on-router-egress, and orphan routers.
- **External traffic** (`moqlab/trafficgen.py`) - one custom Python sender and
  one receiver generate bulk TCP, paced CBR UDP, and scripted segmented TCP.
  Named paths get generated `/32` alias pairs and explicit symmetric routes;
  resolved plans live in each run directory. Containernet only.
- **Synthesis** (`moqlab/config/synth.py`) - turns topology config into
  per-relay moqx YAML files and argv lists for media binaries.
- **Docker backend** (`moqlab/orchestrator/docker_backend.py`) - creates one
  bridge network and one detached container per node. Docker labels are the
  state of truth.
- **Containernet backend** (`moqlab/orchestrator/containernet_backend.py`) -
  creates Docker hosts inside Containernet (routers included, with forwarding
  sysctls), wires one direct host↔host veth pair per `links:` entry (no
  switches), assigns per-node /32 loopbacks + /etc/hosts + static routes,
  applies per-direction tc chains from `orchestrator/shaping.py`, starts node
  binaries explicitly after `net.start()`, opens `CLI(net)`, and tears down
  on CLI exit. Refuses topologies without `links:`.
- **CLI** (`moqlab/cli.py`) - `build moqx`, `build images`, `validate`,
  `run`, `down`, `logs`, `ls`, and `rm pycache`. `up` remains a hidden
  compatibility alias for `run`. `run --vis/--visualize` serves the
  localhost visualizer while the topology is active.
- **Visualizer** (`moqlab/visualizer.py` + `visualizer/`) - dependency-free
  localhost HTTP/API server with separate browser assets under
  `moqlab/visualizer/`. It renders a pannable/zoomable validated topology and
  samples live per-link throughput from active Containernet `mn.<node>`
  interfaces when available and reads the selected media subscriber's atomic
  player-metrics file from its container. Docker-backend runs render topology
  only for links because Docker bridge counters are not per-topology-link.
- **Designer** (`moqlab/designer.py` + `visualizer/designer.*`) - standalone
  dependency-free localhost drag/drop editor launched by `moqlab design`.
  Pydantic JSON Schema drives every config field; a small UI manifest adds
  graph semantics. It imports examples or local YAML, validates through the
  canonical `TopologyConfig`, and downloads normalized YAML without running
  containers or writing repository files.
- **Tests** (`tests/unit/`) - unit tests for schema, synthesis, and
  Containernet launch ordering plus visualizer snapshot/rate helpers. They do
  not require Docker, Containernet, or root.

Media support is clear LOC AV1 spatial-SVC only: one origin per relay tree,
configured namespace/track, headless or direct-X11 Chromium with strict
decoded frame readiness, plus lightweight native `mlmsub` subscribers with
dependency-chain subscription, opt-in chain-correct simulated playback, live
media metrics, and first-media readiness. Not implemented yet:
bandwidth-estimation ABR, temporal SVC, DRM/audio selection,
multiple upstreams per relay, generative topologies,
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
│   ├── Dockerfile.media-pub
│   ├── Dockerfile.media-sub
│   ├── Dockerfile.media-native-sub
│   ├── Dockerfile.traffic
│   └── README.md
├── visualizer/
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   ├── designer.html
│   ├── designer.css
│   └── designer.js
├── moqlab/
│   ├── __main__.py
│   ├── build.py
│   ├── cli.py
│   ├── exceptions.py
│   ├── designer.py
│   ├── visualizer.py
│   ├── trafficgen.py
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
    tls: { insecure: false, generated: true }
    cache: { enabled: false, max_tracks: 100, max_groups_per_track: 3 }
  publisher:
    image: moqlab-media-pub
  subscriber:
    image: moqlab-media-sub
    native_media_image: moqlab-media-native-sub
    media_client: chrome-headless
    native_playback: receive
    log_level: INFO

startup:
  relay_warmup_s: 2.0
  publisher_warmup_s: 1.0
  traffic_ready_timeout_s: 5.0

relays:
  relay-a: { listen_port: 9668, admin_port: 9669, upstream: null }
  relay-b: { listen_port: 9670, admin_port: 9671, upstream: relay-a }

publishers:
  pub: { connects_to: relay-a, asset: testsvc, listen_port: 4443,
         fingerprint_port: 8081 }

subscribers:
  sub: { connects_to: relay-b, namespace: msf/clear, track: video/s2 }

routers:
  rt-1: {}

traffic:
  sender: { id: traffic-tx }
  receiver: { id: traffic-rx }
  routes:
    main: { path: [traffic-tx, rt-1, traffic-rx] }
  flows:
    - { id: load, kind: cbr, route: main, duration_s: 30,
        rate_mbps: 10, packet_size_bytes: 1200 }

links:
  - from: pub
    to: relay-a
    forward: { bandwidth_mbps: 100, delay_ms: 5 }
    reverse: { delay_ms: 5 }
  - from: relay-a
    to: rt-1
    forward: { delay_ms: 20 }
    reverse: { delay_ms: 20 }
  - from: rt-1
    to: relay-b
    forward: { bandwidth_mbps: 50, aqm: dualpi2 }
    reverse: { delay_ms: 20 }
  - from: relay-b
    to: sub
    forward: { bandwidth_mbps: 20, delay_ms: 69 }
    reverse: { delay_ms: 69 }
```

Implemented invariants:

- `topology_mode` must be `explicit`.
- Node ids are globally unique (routers included) and valid as Docker
  names/DNS labels.
- Relay `upstream` and pub/sub `connects_to` references must target known
  relays — never routers.
- A relay has at most one upstream; upstream chains must be cycle-free.
- Relay listen/admin ports must be unique across the topology.
- `startup` warmups are non-negative seconds and config-driven.
- `links` reference known nodes and may not duplicate an undirected pair.
- When `links`/`routers` are declared, every `upstream`/`connects_to` pair
  must be connected through the link graph.
- `aqm` is only valid on directions whose egress node is a router; every
  declared router must appear in at least one link; `jitter_ms` requires
  `delay_ms`.
- Unknown fields are rejected.
- Traffic uses exactly two endpoint containers. Named paths must follow
  declared links with routers as all intermediate nodes; flows select a path.

Future target config work may introduce named `networks`, relay `upstreams`,
scenarios, and generative topology mode. Do not start
that migration without discussion. See [ROUTER.md](ROUTER.md) for the
implemented router/shaping design.

## Containernet Notes

Containernet is Mininet extended with Docker-container hosts. The important
mental model is:

```text
Host machine
└── Containernet process
    ├── Docker host: relay/pub/sub container
    ├── Docker host: router container (ip_forward, owns tc qdiscs)
    └── direct veth pairs connecting those hosts (one per `links:` entry)
```

Applications run inside the Docker hosts, not on the Ubuntu host running
Containernet. Do not use host `localhost` for node-to-node traffic. In the
current backend, nodes should address each other by node id/DNS label, while
the backend manages per-link addresses internally.

Containernet specifics that matter in this repo:

- Use `addDocker(...)` for node containers (routers too; pass `sysctls=` for
  forwarding).
- Do NOT use `TCLink`/`addSwitch`: links are plain veth pairs, and shaping is
  explicit tc commands synthesized by `orchestrator/shaping.py` and run via
  `host.cmd()` inside the owning container.
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
- Every `TopologyConfig` schema change must keep the designer schema-contract
  tests passing. Ordinary fields must remain reachable through generated
  forms; new node collections, relationships, or discriminated variants must
  also update the designer UI manifest and graph interactions.
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
- Images must not contain secrets. Generated media TLS is injected at runtime
  from the run directory and shared only by that run's relays/origin.
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
- Integration tests live under `tests/integration/` and are gated with
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
