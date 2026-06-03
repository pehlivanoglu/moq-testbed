# moqlab — MoQ Multirelay Testbed Orchestrator

`moqlab` takes a single YAML topology and brings it up as a graph of MoQ
relays, publishers, and subscribers. Two backends share the same config:

- **`--backend containernet`** (default for `run`) — each node is a
  Containernet Docker host attached to one or more shaped TCLinks via an OVS
  bridge per edge. Foreground; drops you into the Mininet CLI shell. Exit to
  tear down.
- **`--backend docker`** — each node is a Docker container on a
  user-defined bridge network. Detached; `moqlab down` to tear down.

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
│   ├── build.py                    ← build command planning helpers
│   ├── cli.py                      ← Click commands
│   ├── exceptions.py
│   ├── visualizer.py               ← localhost topology/rate visualizer
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

For Containernet setup and venv choices, see [INSTALL.md](INSTALL.md).

### Python environment

Use one Python environment that can import both moqlab's dependencies and
Containernet's `mininet` package. That venv can live outside this repo in the
Containernet checkout, or inside `moqlab/.venv`; [INSTALL.md](INSTALL.md)
shows both options.

Once the environment is ready, run from the moqlab project directory so
`moqlab/` is importable on the default `sys.path`:

```bash
cd /path/to/moq-testbed/moqlab
python -m pytest -q
python -m moqlab doctor
sudo python -m moqlab run -c configs/examples/linear_3relay.yaml
# inside Mininet CLI:
#   containernet> sub  tail -f /tmp/sub.log
#   containernet> exit
```

Since moqlab is not installed into the venv, edits to the source take effect
immediately on the next `python -m moqlab` invocation. No reinstall, ever.

## Building moqx and node images

Use moqlab build commands instead of hand-running the repo build and Docker
commands:

```bash
cd moqlab

# Builds moqx and prepares moxygen artifacts. If the moxygen artifacts are
# missing, this runs the repository setup step first.
python -m moqlab build moqx

# Builds moqlab-relay, moqlab-pub, and moqlab-sub from the repo root context.
python -m moqlab build images
```

## Quick start

```bash
cd moqlab

# Check Python deps, Docker, images, Containernet importability, and config.
python -m moqlab doctor -c configs/examples/linear_3relay.yaml

# Validate the topology
python -m moqlab validate -c configs/examples/linear_3relay.yaml

# Build local binaries and images once, or after source changes
python -m moqlab build moqx
python -m moqlab build images

# Containernet backend (default; foreground)
sudo /path/to/containernet/venv/bin/python3 -m moqlab run \
     -c configs/examples/linear_3relay.yaml --visualize

# Docker backend (detached)
python -m moqlab run -c configs/examples/linear_3relay.yaml --backend docker --publish-ports --visualize
python -m moqlab ls
python -m moqlab logs --run-id <id> sub -f
python -m moqlab down --run-id <id>
```

`moqlab run` performs the same readiness checks that matter for the selected
backend. If Docker is unavailable, an image is missing, or Containernet is not
importable from the current Python, it exits before starting the topology and
prints the next command to run.

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

`moqlab run` synthesizes one moqx YAML per relay into
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
| `python -m moqlab doctor [-c <config>] [--backend docker\|containernet]` | both | Check Python deps, Docker, required images, Containernet importability, privileges, and optional config readiness. |
| `python -m moqlab build moqx` | n/a | Build moqx and prepare moxygen binaries used by images. |
| `python -m moqlab build images` | n/a | Build `moqlab-relay`, `moqlab-pub`, and `moqlab-sub`. |
| `python -m moqlab validate -c <config>` | both | Parse + validate, no side effects. |
| `python -m moqlab run -c <config> [--backend docker\|containernet] [--run-id N] [--publish-ports] [--vis\|--visualize]` | both | Run topology. Defaults to `containernet`. With `--visualize`, also serves `http://127.0.0.1:8765/` showing the topology graph and link rates. Live per-link throughput is available for Containernet runs, where every topology edge has its own interface. Docker-backend runs still render the correct topology, but Docker's single bridge interface is not split per topology link. |
| `python -m moqlab down --run-id <name>` | docker | Stop and remove containers + network. |
| `python -m moqlab ls` | docker | List active runs. |
| `python -m moqlab logs --run-id <name> [-f] [-n N] <node_id>` | docker | Container logs for one node. |
| `python -m moqlab rm pycaches` | n/a | Remove project `__pycache__` dirs, `.pyc` / `.pyo` files, and `.pytest_cache`, skipping `.venv` and `.runs`. |

`python -m moqlab up ...` is kept as a compatibility alias for `run`.

Global option: `--runs-dir <path>` (or `MOQLAB_RUNS_DIR=…`). Default:
`moqlab/.runs/`.

## Tests

```bash
cd moqlab
.venv/bin/pytest -q          # unit tests, no Docker required
```

`pytest.ini` + `conftest.py` at the project root tell pytest where the test
suite lives and which directory is the import root; the `moqlab/` package on
that root is picked up automatically without any install step.

Coverage: schema validation (uniqueness, upstream resolution, cycles, port
collisions, link dedup, mode gating, log-level validation), relay YAML
synthesis (DNS URLs, override inheritance, multi-relay file emission),
publisher and subscriber argv synthesis (flag composition, optional flags,
endpoint propagation), build command planning, cleanup helpers, startup warmup
validation, and Containernet process launch order.

Integration tests (require Docker + Containernet) are deferred — see
[TODO.md](TODO.md).
