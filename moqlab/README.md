# moqlab — MoQ Multirelay Testbed Orchestrator

`moqlab` takes a single YAML topology and brings it up as a graph of MoQ
relays, publishers, subscribers, and IP routers. Two backends share the same
config:

- **`--backend containernet`** (default for `run`) — every node (including
  each router) is its own Containernet Docker host; every `links:` entry is a
  direct host↔host veth pair (no switches). Routers forward IP and own the
  link queues: per-direction shaping (HTB rate, netem delay/jitter/loss, and
  L4S AQMs like `dualpi2`) is applied with explicit tc commands inside the
  owning container. Foreground; drops you into the Mininet CLI shell. Exit to
  tear down.
- **`--backend docker`** — each node is a Docker container on a
  user-defined bridge network. Detached; `moqlab down` to tear down. Ignores
  `links:`; refuses topologies that declare routers.

No per-relay YAMLs. No hardcoded IPs in configs. No `localhost`. The
orchestrator wires everything by name (Docker DNS on the docker backend;
generated `/etc/hosts` + static routes on containernet), so the same
router-free config runs on either backend.

Publisher/subscriber nodes default to the existing text tools. `kind: media`
selects `mlmpub` and a Chromium-driven WARP Player for clear LOC AV1 spatial
SVC. Readiness requires the configured resolution, non-black pixels, and
changing decoded frame hashes.

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
│   ├── visualizer.py               ← localhost visualizer API/static server
│   ├── config/
│   │   ├── schema.py               ← Pydantic v2 topology model
│   │   └── synth.py                ← topology → relay YAML + pub/sub argv
│   └── orchestrator/
│       ├── docker_backend.py       ← Docker backend
│       ├── containernet_backend.py ← Containernet backend
│       ├── shaping.py              ← DirectionSpec → tc qdisc command chains
│       └── routing.py              ← BFS next hops → `ip route` commands
├── configs/
│   └── examples/
│       ├── linear_1r_1s.yaml      ← 1 relay + pub + sub + 1 router (dualpi2 bottleneck)
│       ├── linear_3r_1s.yaml      ← 3-relay chain with a router between relay hops
│       └── tree_3r_4s.yaml        ← 3-relay tree behind one core router + 4 subscribers
├── docker/                         ← Dockerfiles for the four node images
│   └── README.md
├── visualizer/                      ← browser UI assets (HTML, CSS, JS)
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
sudo python -m moqlab run -c configs/examples/linear_3r_1s.yaml
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

# Builds moqlab-relay, moqlab-pub, moqlab-sub, and moqlab-router from the
# repo root context. The router image compiles a pinned modern iproute2 so
# its tc knows dualpi2; the host's tc version does not matter.
python -m moqlab build images

# Builds mlmpub and WARP Player images from dirty local sibling repos.
python -m moqlab build media-images
```

## Quick start

```bash
cd moqlab

# Check Python deps, Docker, images, Containernet importability, and config.
python -m moqlab doctor -c configs/examples/linear_3r_1s.yaml

# Validate the topology
python -m moqlab validate -c configs/examples/linear_3r_1s.yaml

# Build local binaries and images once, or after source changes
python -m moqlab build moqx
python -m moqlab build images

# Containernet backend (default; foreground)
sudo /path/to/containernet/venv/bin/python3 -m moqlab run \
     -c configs/examples/linear_3r_1s.yaml --visualize

# Docker backend (detached)
python -m moqlab run -c configs/examples/linear_3r_1s.yaml --backend docker --publish-ports --visualize
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

routers:                     # Containernet only; Docker backend refuses
  rt-ab: {}                  # image defaults to defaults.router.image (moqlab-router)
  rt-bc: {}

links:                       # Containernet only; physical wiring + shaping
  - from: pub
    to: relay-a
    forward: { bandwidth_mbps: 100, delay_ms: 5 }   # egress on pub's iface
    reverse: { delay_ms: 5 }                        # egress on relay-a's iface
  - from: relay-a
    to: rt-ab
    forward: { delay_ms: 10 }
    reverse: { delay_ms: 10 }
  - from: rt-ab
    to: relay-b
    forward: { bandwidth_mbps: 50, aqm: dualpi2 }   # bottleneck on router egress
    reverse: { delay_ms: 10 }
  # ... rt-bc, relay-c, sub follow the same pattern
```

Per direction (`forward` = from→to, `reverse` = to→from) you can set
`bandwidth_mbps` (HTB rate), `delay_ms` / `jitter_ms` / `loss_pct` (netem),
and `aqm` (currently `dualpi2`). The qdisc chains these compile to are
documented in [ROUTER.md](ROUTER.md).

Invariants the schema enforces:

| Rule | Why |
|---|---|
| `topology_mode == "explicit"` | Generative mode is deferred. |
| Node ids match `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$` and are globally unique (routers included) | Used as Docker container name + the name other nodes resolve. |
| `upstream` and `connects_to` reference known relays (never routers) | Catches typos before containers start. |
| Single upstream per relay, no cycles | v1 supports linear/tree only. |
| Relay `listen_port` + `admin_port` unique across the topology | Avoids host-port collisions when publishing. |
| Startup warmups are non-negative seconds | Keeps relay bind and publisher namespace timing config-driven. |
| Each undirected link appears at most once | Prevents accidental double-shaping. |
| When `links:`/`routers:` are declared, every `upstream`/`connects_to` pair must have a path through the link graph | A relay that cannot reach its upstream would only fail at run time. |
| `aqm` only on directions whose egress node is a router | Endpoint images ship an iproute2 too old for modern AQMs; the router image carries its own. |
| Every declared router appears in at least one link | An unwired router is a config bug. |
| `jitter_ms` requires `delay_ms` | netem expresses jitter as a variation of delay. |
| Unknown fields rejected | Surfaces typos as `ConfigError`. |

### AV1-SVC media nodes

See [`media_svc_headless.yaml`](configs/examples/media_svc_headless.yaml),
[`media_svc_headed_3r.yaml`](configs/examples/media_svc_headed_3r.yaml), and
[`media_svc_x11.yaml`](configs/examples/media_svc_x11.yaml). Media
topologies generate one ECDSA P-256 certificate per run and mount it into all
relays plus the media origin. The root relay pulls `mlmpub`; Chromium trusts
the same certificate through the publisher's `/fingerprint` endpoint.
Relay-to-relay hops remain encrypted but currently use insecure upstream
verification because moqx does not yet implement `upstream.tls.ca_cert`.

```yaml
defaults:
  relay:
    tls: { insecure: false, generated: true }
  publisher: { media_image: moqlab-media-pub }
  subscriber: { media_image: moqlab-media-sub }

startup:
  media_ready_timeout_s: 30

publishers:
  pub:
    kind: media
    connects_to: relay-a
    asset: testsvc
    listen_port: 4443
    fingerprint_port: 8081

subscribers:
  sub:
    kind: media
    connects_to: relay-c
    namespace: msf/clear
    track: video/s2
    browser_mode: headless
    minimal_buffer_ms: 200
    target_latency_ms: 300
```

`browser_mode: headed` requires a unique `ui_port`. Docker also requires
`--publish-ports`. The visualizer links headed nodes to
`http://127.0.0.1:<ui_port>/vnc.html`; autoplay remains active while noVNC
permits manual `s0/s1/s2` changes.

`browser_mode: x11` opens Chromium directly on host X display. It forbids
`ui_port`; backend mounts `/tmp/.X11-unix` and forwards `DISPLAY`. Grant only
root local-display access, preserve `DISPLAY` through sudo, then revoke access:

```bash
xhost +SI:localuser:root
sudo --preserve-env=DISPLAY ../../../containernet/venv/bin/python3 -m moqlab run \
  -c configs/examples/media_svc_x11.yaml --backend containernet --visualize
xhost -SI:localuser:root
```

Current scope is clear LOC AV1 spatial-only SVC, one temporal layer, manual
quality selection, and one media origin per relay tree. Namespace selection
comes from YAML because current relays do not replay earlier announcements.
The automation runner uses the player's catalog-subscription mode; it does not
depend on a joining `FETCH` being proxied by the relay.
There is no ABR, DRM, audio selection, or temporal SVC.

## How wiring works

`moqlab run` synthesizes one moqx YAML per relay into
`<runs_dir>/<run_id>/configs/<relay>.yaml`, then:

- **Docker backend**: creates bridge network `moqlab_<run_id>`. Starts media
  origins first, relays root-first, text publishers, then subscribers. The
  configured warmups separate those phases. Each container is named after its
  node id; URLs like `moqt://relay-a:9668/moq-relay` and
  `https://relay-c:9672/moq-relay` resolve via Docker DNS.
- **Containernet backend**: requires explicit `links:` wiring. Each link is
  one direct host↔host veth pair with its own /24 out of `10.20.0.0/16`
  (.1 = `from` side, .2 = `to` side); no switches, no controller. Every node
  also gets a canonical /32 on `lo` out of `10.99.0.0/24`; `/etc/hosts` on
  every node maps all peer names to those /32s, and the backend installs
  static /32 routes (BFS over the link graph) so the same name-based URLs
  work across any number of router hops. Routers are plain Docker hosts with
  `net.ipv4.ip_forward=1` (plus `rp_filter=0` and ICMP-redirect suppression)
  that run no MoQ binary. After `net.start()` the backend assigns loopbacks,
  disables NIC offloads on link interfaces (GSO/TSO/GRO would distort
  shaping), installs routes, applies the per-direction tc chains from
  `orchestrator/shaping.py` inside each owning container, sanity-pings every
  application edge, then launches node binaries in the same config-driven
  order and drops into `CLI(net)`.

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
| `python -m moqlab build images` | n/a | Build `moqlab-relay`, `moqlab-pub`, `moqlab-sub`, and `moqlab-router`. |
| `python -m moqlab build media-images [--publisher-context PATH] [--player-context PATH]` | n/a | Build `moqlab-media-pub` and `moqlab-media-sub` from local contexts. Environment equivalents: `MOQLAB_MEDIA_PUBLISHER_CONTEXT` and `MOQLAB_MEDIA_PLAYER_CONTEXT`. |
| `python -m moqlab validate -c <config>` | both | Parse + validate, no side effects. |
| `python -m moqlab run -c <config> [--backend docker\|containernet] [--run-id N] [--publish-ports] [--vis\|--visualize]` | both | Run topology. Defaults to `containernet`. With `--visualize`, also serves `http://127.0.0.1:8765/` showing a pannable/zoomable topology graph and link rates. Live per-link throughput is available for Containernet runs, where every topology edge has its own interface. Docker-backend runs still render the correct topology, but Docker's single bridge interface is not split per topology link. |
| `python -m moqlab down --run-id <name>` | docker | Stop and remove containers + network. |
| `python -m moqlab ls` | docker | List active runs. |
| `python -m moqlab logs --run-id <name> [-f] [-n N] <node_id>` | docker | Container logs for one node. |
| `python -m moqlab rm pycaches` | n/a | Remove project `__pycache__` dirs, `.pyc` / `.pyo` files, and `.pytest_cache`, skipping `.venv` and `.runs`. |

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
collisions, link dedup, link-graph reachability, router/aqm placement rules,
mode gating, log-level validation), tc qdisc chain synthesis, static-route
computation, relay YAML synthesis (DNS URLs, override inheritance,
multi-relay file emission), publisher and subscriber argv synthesis (flag
composition, optional flags, endpoint propagation), build command planning,
cleanup helpers, startup warmup validation, and Containernet build/configure/
launch behavior (router sysctls, addressing, command ordering).

The media Docker acceptance is gated and expects prebuilt media images:

```bash
MOQLAB_INTEGRATION=1 .venv/bin/python -m pytest -q tests/integration/test_media_svc.py
```
