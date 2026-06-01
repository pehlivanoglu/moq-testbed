# moqlab — MoQ Multirelay Testbed Orchestrator

`moqlab` takes a single YAML topology and brings it up as a graph of MoQ
relays, publishers, and subscribers. Two backends share the same config:

- **`--backend docker`** (default) — each node is a Docker container on a
  user-defined bridge network. Detached; `moqlab down` to tear down.
- **`--backend containernet`** — each node is a Containernet Docker host
  attached to one or more shaped TCLinks via an OVS bridge per edge.
  Foreground; drops you into the Mininet CLI shell. Exit to tear down.

No per-relay YAMLs. No hardcoded IPs. No `localhost`. The orchestrator wires
everything by container DNS, so the same config runs on either backend.

## Layout

```
moqlab/
├── requirements.txt                ← runtime deps (pydantic, click, pyyaml, docker)
├── requirements-dev.txt            ← + pytest, for the dev venv
├── pytest.ini                      ← pytest config (testpaths, addopts)
├── conftest.py                     ← empty; marks the pytest rootdir
├── README.md                       ← you are here
├── AGENTS.md                       ← guide for AI agents touching this package
├── TODO.md                         ← deferred / future work
├── moqlab/                         ← package source (PEP 420 namespace pkg, no __init__.py)
│   ├── __main__.py                 ← `python -m moqlab` entry point
│   ├── cli.py                      ← Click commands
│   ├── exceptions.py
│   ├── config/
│   │   ├── schema.py               ← Pydantic v2 topology model
│   │   └── synth.py                ← topology → relay YAML + pub/sub argv
│   └── orchestrator/
│       ├── docker_backend.py       ← Docker backend
│       └── containernet_backend.py ← Containernet backend
├── configs/
│   └── examples/
│       └── linear_3relay.yaml      ← 3 relays + pub + sub + shaped links
├── docker/                         ← Dockerfiles for the three node images
│   └── README.md
└── tests/unit/                     ← pytest, no Docker required
```

There is no `pyproject.toml` and no `setup.py`. moqlab is not a pip-installable
library — it's a directory you run with `python -m moqlab`.

## Running it

There is no install step. moqlab is a directory of Python files; you run it
in place with `python -m moqlab`. You only need to make sure the four
runtime deps (`pydantic`, `click`, `PyYAML`, `docker`) are available to the
interpreter you invoke.

### Day-to-day dev venv (Docker backend, tests)

```bash
cd moqlab
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m moqlab --help
.venv/bin/pytest -q
```

### Containernet backend

Containernet ships its own venv (its `mininet` install lives only there).
moqlab itself doesn't need installing — only its deps do. Once per machine:

```bash
sudo /path/to/containernet/venv/bin/pip install -r \
     /path/to/moq-testbed/moqlab/requirements.txt
```

Then from the moqlab project directory (so `moqlab/` is importable on the
default sys.path):

```bash
cd /path/to/moq-testbed/moqlab
sudo /path/to/containernet/venv/bin/python3 -m moqlab up \
     -c configs/examples/linear_3relay.yaml --backend containernet
# inside Mininet CLI:
#   containernet> sub  tail -f /tmp/sub.log
#   containernet> exit
```

Two venvs, different jobs:

| Venv | What it's for |
|---|---|
| `moqlab/.venv` | `pytest`, `python -m moqlab up --backend docker`, day-to-day dev |
| Containernet venv (e.g. `~/Research/Repos/containernet/venv`) | `sudo python -m moqlab up --backend containernet` |

Since moqlab isn't installed into either venv, edits to the source take
effect immediately on the next `python -m moqlab` invocation. No reinstall,
ever.

## Building the node images

Once, before the first `up` (see [docker/README.md](docker/README.md)):

```bash
docker build -f moqlab/docker/Dockerfile.relay -t moqlab-relay ../..
docker build -f moqlab/docker/Dockerfile.pub   -t moqlab-pub   ../..
docker build -f moqlab/docker/Dockerfile.sub   -t moqlab-sub   ../..
```

## Quick start

```bash
cd moqlab

# Validate the topology
python -m moqlab validate -c configs/examples/linear_3relay.yaml

# Docker backend (detached)
python -m moqlab up   -c configs/examples/linear_3relay.yaml --backend docker --publish-ports
python -m moqlab ls
python -m moqlab logs --run-id <id> sub -f
python -m moqlab down --run-id <id>

# Containernet backend (foreground; see "Running it" above)
sudo /path/to/containernet/venv/bin/python3 -m moqlab up \
     -c configs/examples/linear_3relay.yaml --backend containernet
```

## Topology schema

```yaml
topology_mode: explicit

defaults:
  relay:      { image: moqlab-relay, endpoint: /moq-relay,
                tls: { insecure: true },
                cache: { enabled: false, max_tracks: 100, max_groups_per_track: 3 } }
  publisher:  { image: moqlab-pub,   insecure: true, log_level: INFO }
  subscriber: { image: moqlab-sub,   insecure: true, log_level: INFO }

startup:
  relay_warmup_s: 2.0
  publisher_warmup_s: 1.0

relays:
  relay-a: { listen_port: 9668, admin_port: 9669, upstream: null }
  relay-b: { listen_port: 9670, admin_port: 9671, upstream: relay-a }
  relay-c: { listen_port: 9672, admin_port: 9673, upstream: relay-b }

publishers:
  pub:     { connects_to: relay-a, namespace: moq-date }

subscribers:
  sub:     { connects_to: relay-c, namespace: moq-date, track: date }

links:                       # Containernet only; Docker backend ignores
  - { from: pub,     to: relay-a, bandwidth_mbps: 100, delay_ms:  5 }
  - { from: relay-a, to: relay-b, bandwidth_mbps:  50, delay_ms: 20 }
  - { from: relay-b, to: relay-c, bandwidth_mbps:  50, delay_ms: 20 }
  - { from: relay-c, to: sub,     bandwidth_mbps:  20, delay_ms: 69 }
```

Invariants the schema enforces:

| Rule | Why |
|---|---|
| `topology_mode == "explicit"` | Generative mode is deferred. |
| Node ids match `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$` and are globally unique | Used as Docker container name + DNS label. |
| `upstream` and `connects_to` reference known relays | Catches typos before containers start. |
| Single upstream per relay, no cycles | v1 supports linear/tree only. |
| Relay `listen_port` + `admin_port` unique across the topology | Avoids host-port collisions when publishing. |
| Startup warmups are non-negative seconds | Keeps relay bind and publisher namespace timing config-driven. |
| Each undirected link appears at most once | Prevents accidental double-shaping. |
| Unknown fields rejected | Surfaces typos as `ConfigError`. |

## How wiring works

`moqlab up` synthesizes one moqx YAML per relay into
`<runs_dir>/<run_id>/configs/<relay>.yaml`, then:

- **Docker backend**: creates bridge network `moqlab_<run_id>`. Starts relays
  upstream-first, waits `startup.relay_warmup_s`, starts publishers, waits
  `startup.publisher_warmup_s`, then starts subscribers. Each container is
  named after its node id; URLs like `moqt://relay-a:9668/moq-relay` and
  `https://relay-c:9672/moq-relay` resolve via Docker DNS.
- **Containernet backend**: builds one OVS bridge per topology edge and
  attaches both endpoints with `TCLink` for shaping. Same DNS-by-name URLs.
  After `net.start()`, it explicitly launches node binaries in the same
  config-driven order and drops into `CLI(net)` after the node processes are
  started.

State of truth for the Docker backend is Docker labels
(`moqlab.run_id=<id>`); `ls` / `down` re-derive from there, so deleting the
run dir never strands containers. The Containernet backend tears down at
CLI(net) exit; there is no detached state.

## CLI reference

Every command is `python -m moqlab <subcommand>`. The `python` interpreter
needs the four runtime deps installed (see "Running it").

| Command | Backend | Purpose |
|---|---|---|
| `python -m moqlab validate -c <config>` | both | Parse + validate, no side effects. |
| `python -m moqlab up -c <config> [--backend docker\|containernet] [--run-id N] [--publish-ports]` | both | Bring topology up. |
| `python -m moqlab down --run-id <name>` | docker | Stop and remove containers + network. |
| `python -m moqlab ls` | docker | List active runs. |
| `python -m moqlab logs --run-id <name> [-f] [-n N] <node_id>` | docker | Container logs for one node. |

Global option: `--runs-dir <path>` (or `MOQLAB_RUNS_DIR=…`). Default:
`moqlab/.runs/`.

## Tests

```bash
cd moqlab
.venv/bin/pytest -q          # 36 unit tests, no Docker required
```

`pytest.ini` + `conftest.py` at the project root tell pytest where the test
suite lives and which directory is the import root; the `moqlab/` package on
that root is picked up automatically without any install step.

Coverage: schema validation (uniqueness, upstream resolution, cycles, port
collisions, link dedup, mode gating, log-level validation), relay YAML
synthesis (DNS URLs, override inheritance, multi-relay file emission),
publisher and subscriber argv synthesis (flag composition, optional flags,
endpoint propagation), startup warmup validation, and Containernet process
launch order.

Integration tests (require Docker + Containernet) are deferred — see
[TODO.md](TODO.md).
