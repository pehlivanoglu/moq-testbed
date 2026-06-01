# AGENTS.md — MOQ Multirelay Testbed (`moqlab`)

This file is the authoritative guide for AI coding agents working on this repository.
Read it completely before touching any file. Every section is load-bearing.

You are a senior researcher and engineer focused on ultra low latency streaming, RTC systems, CDNs, multirelay, and Media over QUIC. Your goal is to help me create a multirelay MOQ testbed.

---

## Important Notes
IMPORTANT NOTE 1: Most folders represent packages or components and usually contain TODO.md and AGENTS.md files. If you are going to work on a component, you must read both files first. After making changes in that component, you must also update the relevant Markdown documentation, including TODO.md, AGENTS.md, and any other related docs. For example, mark completed items as done, add new notes, or update existing information when needed. If you do not fully implement something, do not leave it undocumented. Add it to TODO.md, future work, or missing features so we do not forget it later.Explicitly document those as missing/future work items. Before implementing something that is intentionally left incomplete or deferred, ask me first.

IMPORTANT NOTE 2: You are not allowed to make your own decisions without asking it to me. We can discuss everything if needed, but you must explain and get my permission first.

IMPORTANT NOTE 3: I dont want any hardcoded or CLI argument configs. I want you to use config files for ever component.

## Project identity

`moqlab` is a PhD research testbed for **Media over QUIC (MoQT)** multirelay experiments.
It lets a researcher define arbitrary CDN-like relay topologies in YAML, instantiate them
as Docker containers connected by emulated networks via **Containernet/Mininet**, run
time-sequenced network impairment scenarios, and collect structured logs and QUIC-level
traces for analysis.

This is a long-lived research tool. Every decision should optimise for
**correctness, reproducibility, and researcher ergonomics**.

---

## Current implementation status (as of 2026-05-29)

The Python package described below is being delivered in phases. The slice
that exists today:

- `moqlab/{requirements.txt, requirements-dev.txt, pytest.ini, conftest.py}` —
  no `pyproject.toml` / no install; run with `python -m moqlab`
- `moqlab/moqlab/{__main__.py, cli.py, exceptions.py}`
- `moqlab/moqlab/config/{schema.py, synth.py}` — topology schema + relay-YAML / pub-argv / sub-argv synthesis
- `moqlab/moqlab/orchestrator/{docker_backend.py, containernet_backend.py}`
- `moqlab/configs/examples/linear_3relay.yaml` — 3 relays + 1 pub + 1 sub + 4 shaped links
- `moqlab/docker/{Dockerfile.relay, Dockerfile.pub, Dockerfile.sub, README.md}` — image-build inputs only; no orchestration here
- `moqlab/tests/unit/{test_config_schema.py, test_synth.py, test_containernet_backend.py}` — 36 unit tests
- `moqlab/{README.md, AGENTS.md, TODO.md}`

What works end-to-end today:

- **Docker backend** — `moqlab up --backend docker` brings up the full
  topology (relays + pubs + subs) on a bridge network; detached;
  `moqlab down` to tear down.
- **Containernet backend** — `moqlab up --backend containernet` builds the
  same topology with `TCLink` per edge (bw / delay / jitter / loss from the
  `links:` block), explicitly launches relay/pub/sub binaries after
  `net.start()`, drops into `CLI(net)`, tears down on exit.

What is NOT implemented (tracked in [moqlab/TODO.md](moqlab/TODO.md)):

- Multiple upstreams per relay (mesh / fan-in).
- Generative topologies (`topology_mode: generative`).
- Scenario runner, observability (JSONL collector, Prometheus, QLOG),
  TLS CA per run, full `experiments/run_*/` archive layout.

For day-to-day implementation rules read [moqlab/AGENTS.md](moqlab/AGENTS.md);
the remainder of this file is the **target** architecture for future phases.

---

## Canonical repository layout

```
A new directory in moqx repo:
moqlab/
├── AGENTS.md                        ← you are here
├── README.md
├── pyproject.toml                   ← moqlab Python package definition
├── requirements.txt                 ← pinned deps for the host environment
├── Makefile                         ← common dev tasks (see §Commands)
│
├── moqlab/                          ← main Python package
│   ├── __init__.py
│   ├── cli.py                       ← Click CLI entry point (`moqlab` command)
│   │
│   ├── config/
│   │   ├── __init__.py
│   │   ├── schema.py                ← Pydantic v2 models for topology YAML
│   │   ├── validator.py             ← semantic validation (connectivity, port clashes)
│   │   └── generator.py            ← generative topology mode (seed-based)
│   │
│   ├── orchestrator/
│   │   ├── __init__.py
│   │   ├── topology.py             ← Containernet topology builder
│   │   ├── links.py                ← tc/netem link abstraction
│   │   ├── vnf.py                  ← VNF hot-insert / hot-remove
│   │   └── certs.py                ← TLS cert generation (self-signed CA per run)
│   │
│   ├── scenario/
│   │   ├── __init__.py
│   │   ├── runner.py               ← time-sequenced scenario event loop
│   │   ├── netem.py                ← tc qdisc netem command builder
│   │   └── ecn.py                  ← ECN / L4S kernel flag helpers
│   │
│   ├── observability/
│   │   ├── __init__.py
│   │   ├── collector.py            ← tails container stdout into per-node JSONL files
│   │   ├── archiver.py             ← seals experiment directory at end of run
│   │   └── prometheus.py           ← spins up Prometheus+Grafana sidecar
│   │
│   └── analysis/
│       ├── __init__.py
│       └── metrics.py              ← post-experiment metric extraction helpers
│
├── docker/
│   ├── relay/
│   │   ├── Dockerfile              ← moqx relay image
│   │   └── entrypoint.sh
│   ├── publisher/
│   │   ├── Dockerfile              ← moxygen publisher image
│   │   └── entrypoint.sh
│   ├── subscriber/
│   │   ├── Dockerfile              ← moxygen headless subscriber image
│   │   └── entrypoint.sh
│   └── browser-sub/
│       ├── Dockerfile              ← Chromium + Playwright browser subscriber
│       └── entrypoint.sh
│
├── configs/
│   ├── examples/
│   │   ├── linear_3relay.yaml      ← minimal working example (start here)
│   │   ├── tree_topology.yaml
│   │   ├── mesh_topology.yaml
│   │   └── generative_example.yaml
│   └── scenarios/
│       ├── baseline.yaml           ← no impairments
│       ├── sudden_loss.yaml
│       ├── latency_spike.yaml
│       ├── bandwidth_step.yaml
│       └── l4s_enabled.yaml
│
├── experiments/                    ← gitignored; created at runtime
│   └── run_<YYYYMMDD>_<NNN>/      ← one directory per run (see §Experiment layout)
│
├── tests/
│   ├── unit/
│   │   ├── test_config_schema.py
│   │   ├── test_generator.py
│   │   └── test_netem.py
│   └── integration/
│       └── test_linear_topology.py ← requires Containernet host
│
└── docs/
    ├── topology_format.md
    ├── scenario_format.md
    └── observability.md
```

Do not create files outside this layout without asking to me and updating this document.

---

## Technology stack and roles

| Component | Technology | Role |
|---|---|---|
| Relay | [moqx](https://github.com/openmoq/moqx) (C++) | MoQT relay; Docker container for each |
| MOQ library | [moxygen](https://github.com/openmoq/moxygen) (C++) | Docker container for eachPublisher and headless subscriber; Docker container for each |
| Network emulation | Containernet + Mininet | Container topology + virtual links |
| Link impairment | `tc qdisc netem` | Per-link BW / delay / loss / jitter / ECN |
| Config language | YAML (PyYAML + Pydantic v2) | Topology and scenario definition |
| Orchestration | Python 3.11+ (Click CLI) | Topology build, scenario run, archiving |
| Observability | JSONL logs + QLOG + Prometheus + Grafana | Live and post-hoc analysis |
| Subscribers | Native just for receive and decode, browser to play the video  | WebTransport/QUIC subscriber automation |
| TLS | Self-signed CA per run (cryptography lib) | Required for WebTransport |

**Never introduce a dependency not in this table without discussion.**
If a new dependency is genuinely needed, add it to `requirements.txt` with a pinned version
and update this table.

---

## Config format (read this carefully)

All topology configs live in `configs/`. Two modes exist, selected by `topology_mode`.

### Mode A — explicit

```yaml
topology_mode: explicit

startup:
  relay_warmup_s: 2.0                 # wait after relays start before pubs
  publisher_warmup_s: 1.0             # wait after pubs start before subs

relays:
  relay_1:
    image: moqlab/relay:latest         # Docker image tag
    proto: [webtransport, quic]        # protocols to accept
    port: 4433                         # QUIC/WT listen port (must be unique per host)
    upstreams:                         # relays this relay connects TO (fan-out)
      relay_2: net_r1r2
      relay_3: net_r1r3

  relay_2:
    image: moqlab/relay:latest
    proto: [webtransport, quic]
    port: 4434
    upstreams: {}

  relay_3:
    image: moqlab/relay:latest
    proto: [webtransport, quic]
    port: 4435
    upstreams: {}

networks:
  net_pub_r1:                          # name referenced by publishers/relays
    bandwidth_mbps: 50
    delay_ms: 5
    delay_jitter_ms: 1
    loss_pct: 0.0
    ecn: false
    l4s: false

  net_r1r2:
    bandwidth_mbps: 10
    delay_ms: 20
    delay_jitter_ms: 2
    loss_pct: 0.1
    ecn: true
    l4s: false

  net_r1r3:
    bandwidth_mbps: 10
    delay_ms: 30
    delay_jitter_ms: 0
    loss_pct: 0.0
    ecn: false
    l4s: false

publishers:
  pub_1:
    image: moqlab/publisher:latest
    type: vod                          # vod | live
    source: /media/bbb_360p.fmp4      # path inside container (bind-mounted)
    connects_to: relay_1
    network: net_pub_r1
    namespace: "moqlab/video"         # MoQT namespace

subscribers:
  sub_headless_{1..5}:                # range syntax expands to sub_headless_1 … sub_headless_5
    image: moqlab/subscriber:latest
    type: headless
    connects_to: relay_2
    network: net_r2sub                # must be defined in networks block
    decode: false                     # skip decode for pure latency measurement
    metrics: [startup_latency, object_latency, loss_rate, reorder_rate]

  sub_browser_1:
    image: moqlab/browser-sub:latest
    type: browser
    connects_to: relay_3
    network: net_r3sub
```

### Mode B — generative

```yaml
topology_mode: generative
seed: 42                              # integer; controls all randomness; required

relays: 5
topology: tree                        # linear | tree | mesh | random
fanout: 2                             # used when topology: tree
connection_density: 0.4              # used when topology: mesh or random; 0.0–1.0

publishers: 2
publisher_type: vod
subscribers: 10
subscriber_type: headless

default_network:
  bandwidth_mbps: 10
  delay_ms: 20
  delay_jitter_ms: 2
  loss_pct: 0.0
  ecn: false
  l4s: false
```

**Rules for agents editing configs:**
- Every network name referenced by a relay, publisher, or subscriber must be declared in
  the `networks` block. The validator will catch missing references but do not rely on it
  as a substitute for careful editing.
- Startup warmups are seconds and must be non-negative. Keep launch timing in
  the topology config, not in CLI flags or backend constants.
- Ports must be unique across all relay containers on the same host.
- Range syntax `{N..M}` is only valid in subscriber names, not relay or publisher names.
- `l4s: true` requires `ecn: true` on the same network block.
- Do not add YAML anchors (`&`, `*`) — the schema does not support them and they obscure
  config diffs in git.

---

## Scenario format

Scenarios are separate YAML files applied on top of a running topology.

```yaml
scenario:
  - at: 0s
    action: set_link
    link: net_r1r2
    bandwidth_mbps: 10
    delay_ms: 20
    loss_pct: 0.0

  - at: 30s
    action: set_link
    link: net_r1r2
    delay_ms: 120           # sudden latency spike; other params unchanged

  - at: 60s
    action: add_loss
    link: net_r1r2
    loss_pct: 5.0

  - at: 90s
    action: restore
    link: net_r1r2          # restore to values from the topology config

  - at: 120s
    action: add_node
    node:
      id: vnf_probe_1
      image: moqlab/vnf-probe:latest
      attach_between: [relay_1, relay_2]
      network: net_r1r2

  - at: 180s
    action: remove_node
    node_id: vnf_probe_1
```

Valid `action` values: `set_link`, `add_loss`, `restore`, `add_node`, `remove_node`,
`set_ecn`, `set_l4s`. See `docs/scenario_format.md` for the full schema.

Time values are strings: `30s`, `2m`, `90s`. The scenario runner parses them with
`moqlab.scenario.runner.parse_duration()` — do not use bare integers.

---

## Experiment layout (runtime output)

Each run produces one directory under `experiments/`. Never commit this directory.

```
experiments/run_20240315_001/
├── RUN_ID                           # plaintext: "run_20240315_001"
├── config.yaml                      # exact copy of the topology config used
├── scenario.yaml                    # exact copy of the scenario used (or empty)
├── topology_resolved.yaml           # if generative mode: fully expanded explicit graph
├── git.json                         # {moqlab: <hash>, moqx: <hash>, moxygen: <hash>}
├── host_info.json                   # kernel version, CPU, RAM, Docker version
├── logs/
│   ├── relay_1.jsonl
│   ├── relay_2.jsonl
│   ├── pub_1.jsonl
│   └── sub_headless_1.jsonl  …
├── qlog/
│   ├── relay_1_server_<cid>.sqlog
│   └── relay_1_client_<cid>.sqlog  …
├── metrics/
│   └── prometheus_snapshot.tar.gz
└── SEALED                           # written last; presence = run completed cleanly
```

`SEALED` is a zero-byte sentinel written by `archiver.py` after all containers have
exited and all logs have been flushed. A run directory without `SEALED` is incomplete.
Do not post-process or compare incomplete runs.

---

## JSONL log schema

Every node emits one JSON object per line to stdout. The collector tails each container's
stdout and writes it to `logs/<node_id>.jsonl`. All log lines must conform to:

```json
{
  "ts":        1710000000.123,   // float; Unix epoch seconds with ms precision
  "run_id":    "run_20240315_001",
  "node":      "relay_1",
  "node_type": "relay",          // relay | publisher | subscriber
  "event":     "object_forwarded",
  "level":     "info",           // debug | info | warn | error
  // ... event-specific fields
}
```

**Required fields**: `ts`, `run_id`, `node`, `node_type`, `event`, `level`.
All other fields are event-specific. Do not rename or remove required fields.

Important event types and their extra fields:

| `event` | Node type | Extra fields |
|---|---|---|
| `object_received` | relay, subscriber | `track`, `group_id`, `object_id`, `size_bytes`, `receive_delay_ms` |
| `object_forwarded` | relay | `track`, `group_id`, `object_id`, `upstream_id`, `queue_depth` |
| `object_published` | publisher | `track`, `group_id`, `object_id`, `size_bytes` |
| `subscribe_received` | relay | `track`, `subscriber_id` |
| `subscribe_sent` | relay | `track`, `upstream_id` |
| `connection_opened` | any | `peer_addr`, `proto` |
| `connection_closed` | any | `peer_addr`, `reason` |
| `startup_latency` | subscriber | `track`, `first_object_ts`, `subscribe_ts`, `latency_ms` |

Never log raw binary data or TLS key material.

---

## Coding conventions

### Python (moqlab package)

- Python 3.11+. Use `match` statements where they improve clarity.
- **Pydantic v2** for all config models. Use `model_validator` for cross-field checks.
  Never use raw `dict` for structured config data — always deserialise into a model first.
- Type-annotate every function signature. No `Any` without a comment explaining why.
- Exceptions: define domain exceptions in `moqlab/exceptions.py`. Never raise bare
  `Exception` or `RuntimeError`. Catch specific types.
- Async: the scenario runner and log collector are async (`asyncio`). Do not mix
  `threading` and `asyncio` in the same module. Use `asyncio.subprocess` for container
  interaction, not `subprocess.Popen`.
- Logging: use Python `structlog` for all internal moqlab logging (not print, not stdlib
  logging). Bind `run_id` to the structlog context at the start of every run.
- Containernet calls go through `moqlab/orchestrator/topology.py`. Never call Containernet
  or Mininet APIs directly from CLI code or scenario runners.
- The `netem.py` module builds `tc` shell commands as strings and executes them via
  `asyncio.subprocess`. Every command must be logged at `debug` level before execution.
  Do not shell-escape manually — use `shlex.join()`.

### C++ (moxygen and moqx, if modified)

- Follow the existing moxygen and moqx style.
- Build with the same compiler and flags as the Docker image. Do not build moxygen natively on the host for development — always
  build inside the Docker image to avoid environment skew.
- Metric emission: subscribers must write JSON lines to stdout (not stderr). The collector
  only tails stdout.

### Docker images

- Every `Dockerfile` must pin its base image to a full digest
  (`FROM ubuntu:22.04@sha256:<digest>`), not a floating tag.
- Multi-stage builds: build stage compiles; runtime stage copies only the binary.
- No secrets in images. TLS certs are injected at runtime via bind mount from the run
  directory.
- All images must accept configuration via environment variables. Entrypoint scripts read
  env vars and pass them as CLI flags to the binary.
- Image names follow the pattern `moqlab/<role>:<moqx-or-moxygen-commit-short>`. The
  `latest` tag is only for local development.

---

## Commands

```bash
# Install moqlab in editable mode (run once)
pip install -e ".[dev]"

# Run a topology (topology + scenario are separate)
moqlab run --config configs/examples/linear_3relay.yaml \
           --scenario configs/scenarios/sudden_loss.yaml \
           --out experiments/

# Generate a random topology and write it out (does NOT run)
moqlab topo generate --relays 5 --topology mesh --density 0.4 --seed 42 \
                     --out /tmp/generated.yaml

# Validate a config file without running
moqlab validate --config configs/examples/linear_3relay.yaml

# Replay (re-apply scenario to a new run without rebuilding topology)
moqlab replay --run experiments/run_20240315_001 \
              --scenario configs/scenarios/latency_spike.yaml

# Post-experiment analysis
moqlab analyze --run experiments/run_20240315_001 \
               --metric startup_latency object_latency \
               --out experiments/run_20240315_001/analysis.json

# Build all Docker images (requires Docker buildx)
make images

# Run unit tests (no Containernet required)
make test-unit

# Run integration tests (requires Containernet host, runs as root)
sudo make test-integration

# Lint
make lint          

# Clean experiment directories older than N days
make clean-runs DAYS=30
```

---

## How to add a new node type

1. Add the Pydantic model in `moqlab/config/schema.py` following the pattern of
   existing node types. Add it to the `TopologyConfig.nodes` discriminated union.
2. Add a `Dockerfile` under `docker/<node-type>/`.
3. Add the node instantiation logic in `moqlab/orchestrator/topology.py` —
   specifically in `TopologyBuilder._add_node()`.
4. Add a JSONL log event for the node's main action in `docs/observability.md`.
5. Add a unit test in `tests/unit/test_config_schema.py` covering valid and invalid
   configs for the new node type.
6. Add an example config in `configs/examples/` that uses the new node type.
7. Update this `AGENTS.md` table in §Technology stack if the new type introduces a
   new dependency.

Do not skip steps 4–7. Incomplete additions cause silent failures in experiments.

---

## How to add a new scenario action

1. Add the action model in `moqlab/scenario/runner.py` (Pydantic discriminated union
   on the `action` field).
2. Implement the handler as an `async` method on `ScenarioRunner`. Name it
   `_handle_<action_name>()`.
3. If the action touches `tc`, add the command builder to `moqlab/scenario/netem.py`.
4. Write a unit test in `tests/unit/test_netem.py` that verifies the correct `tc`
   command string is produced.
5. Add the action to the valid values table in `docs/scenario_format.md`.

---

## Reproducibility contract

Every experiment run must be fully reproducible from its `experiments/run_*/` directory.
This is a hard requirement, not a nice-to-have.

Agents must not break these invariants:

1. **Seed propagation**: `generative` mode must seed Python's `random` module AND NumPy's
   RNG with the config's `seed` value before generating any topology. The resolved
   explicit graph is written to `topology_resolved.yaml` before any container starts.

2. **Git hash recording**: `archiver.py` records the git HEAD of `moqlab`, the pinned
   commit of the `moqx` submodule, and the pinned commit of the `moxygen` submodule into
   `git.json`. If any of these is dirty (uncommitted changes), the run is **aborted with
   an error**, not a warning. Use `moqlab run --allow-dirty` to override (only for
   development, never for results you intend to publish).

3. **Image digest recording**: `host_info.json` includes the full `RepoDigest` of every
   Docker image used in the run.

4. **Scenario timing**: The scenario runner uses `time.monotonic()` for inter-event
   sleep, not wall clock. Start time is recorded as a Unix timestamp in the run metadata.

5. **tc command recording**: Every `tc` command executed is appended to
   `experiments/run_*/netem.log` in execution order with its timestamp.

---

## L4S and ECN notes

L4S / ECN support is gated by the host kernel version and the QUIC stack in moqx.

- ECN marking in `netem` uses `tc qdisc add dev <iface> root netem ecn`. This flag
  enables CE (Congestion Experienced) marking on packets that would otherwise be dropped.
  Only set this when `ecn: true` in the network config block.
- L4S requires the host kernel ≥ 5.17 and the `sch_fq` or `sch_cake` scheduler with
  L4S mode enabled. Check `moqlab/scenario/ecn.py` for the exact kernel feature probes
  before enabling.
- If `l4s: true` is set but the kernel probe fails, the run is aborted with a descriptive
  error. Do not silently fall back to plain ECN.
- moqx's ECN support depends on its underlying QUIC library. Check
  `docker/relay/Dockerfile` for which QUIC library is linked and verify ECN is enabled at
  build time. This is documented in `docs/observability.md` under "ECN verification".

---

## TLS and WebTransport

WebTransport requires HTTPS. The testbed uses a per-run self-signed CA.

- `moqlab/orchestrator/certs.py` generates a CA and per-node leaf certificates at the
  start of each run using the Python `cryptography` library.
- Certs are written to `experiments/run_*/certs/` and bind-mounted read-only into each
  container at `/etc/moqlab/certs/`.
- The CA certificate is written to `experiments/run_*/certs/ca.crt`. For browser
  subscribers, Playwright injects this CA via `--ignore-certificate-errors-spki-list`
  or the browser's cert store — see `docker/browser-sub/entrypoint.sh`.
- **Never reuse certs across runs.** New certs are generated fresh for every run.
- Certificate lifetimes are set to 24 hours. Experiments longer than 24 hours are not
  currently supported.

---

## Observability stack details

The Prometheus + Grafana sidecar runs on the **host** (not inside Containernet) and is
started by `moqlab/observability/prometheus.py` before containers are launched.

- Prometheus listens on `localhost:9090`.
- Grafana listens on `localhost:3000`. Default credentials: `admin / moqlab`.
- Each relay, publisher, and subscriber container exposes `/metrics` on a sidecar port
  (container port 9091 by default, mapped to a unique host port by the topology builder).
- The Prometheus scrape config is generated dynamically from the resolved topology and
  written to `experiments/run_*/prometheus.yml`.
- Do not hardcode port numbers in Prometheus config. The topology builder assigns them and
  writes the scrape config.

QLOG files are written by moqx and moxygen to `/tmp/qlog/` inside each container.
The collector copies them to `experiments/run_*/qlog/` after the run. QLOG can be large
(hundreds of MB for long runs with many connections) — do not commit them to git.

---

## Testing policy

- **Unit tests** (`tests/unit/`) must not require Docker, Containernet, or root. They
  test config parsing, validation, topology generation, netem command building, and
  metric extraction only. They must pass with `pytest tests/unit/` on a developer laptop.

- **Integration tests** (`tests/integration/`) require a Containernet-capable host and
  must be run as root (`sudo pytest tests/integration/`). They are not run in CI by
  default. They spin up a minimal 2-relay linear topology, run a 30-second baseline
  scenario, and assert that JSONL logs contain the expected event types.

- Test files are named `test_<module>.py` matching the module they test.
  Do not create test files named `test_misc.py` or `test_utils.py`.

- When adding a feature, write the unit test first. If you cannot write a unit test for
  the feature (because it requires Containernet), write it as an integration test and note
  why in a comment at the top of the test function.

---

## What agents must NOT do

- Do not modify `experiments/` in any way. It is runtime output, not source.
- Do not add `print()` statements to `moqlab/` package code. Use `structlog`.
- Do not hardcode IP addresses anywhere. The topology builder assigns IPs from
  `10.0.0.0/8` and they must be read from the Containernet API at runtime.
- Do not run experiments or build Docker images as part of answering a code question.
  Propose the code change; let the researcher run it.
- Do not add `sudo` calls inside Python code. Containernet operations that require root
  are expected to run under `sudo` at the process level (the researcher invokes
  `sudo moqlab run ...`).
- Do not modify pinned dependency versions in `requirements.txt` without an explicit
  instruction to do so. Version changes break reproducibility.
- Do not introduce global mutable state in the `moqlab` package. Configuration and run
  context are passed explicitly through function arguments or Pydantic models.
- Do not write shell scripts for tasks that can be expressed in Python. The only shell
  scripts allowed are Docker entrypoints in `docker/*/entrypoint.sh`.
- Do not access the internet from any code path that runs during an experiment. All
  dependencies must be pre-installed in Docker images.

---

## Glossary

| Term | Meaning in this codebase |
|---|---|
| **run** | One execution of `moqlab run`; produces one `experiments/run_*` directory |
| **topology** | The graph of relays, publishers, and subscribers defined in the config |
| **network block** | A named set of tc/netem parameters applied to one virtual link |
| **scenario** | A time-sequenced list of network events applied to a running topology |
| **node** | Any container in the topology (relay, publisher, subscriber, VNF) |
| **upstream** | The relay a given relay connects TO (fan-out direction) |
| **QLOG** | QUIC event logging format (RFC 9473); produced by moqx and moxygen |
| **SEALED** | Zero-byte sentinel file written by archiver when a run completes cleanly |
| **VNF** | Virtual Network Function; an arbitrary container inserted into a link mid-run |
| **headless subscriber** | moxygen subscriber with no display; gathers metrics only |
| **MoQT** | Media over QUIC Transport (the IETF draft protocol) |

---

*Last updated: initial version. Update this file whenever the layout, conventions, or
contracts described here change. An outdated AGENTS.md is worse than none.*
