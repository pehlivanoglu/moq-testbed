# moqlab — MoQ Multirelay Testbed Orchestrator

`moqlab` takes a single YAML topology and brings it up as a graph of MoQ
relays, publishers, subscribers, IP routers, and optional external traffic. Two backends share the same
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
  `links:`; refuses topologies that declare routers or external traffic.

No per-relay YAMLs. No hardcoded IPs in configs. No `localhost`. The
orchestrator wires everything by name (Docker DNS on the docker backend;
generated `/etc/hosts` + static routes on containernet), so the same
router/traffic-free config runs on either backend.

Publisher/subscriber nodes are media-only: `mlmpub` plus either
Chromium-driven WARP Player or native `mlmsub` for clear LOC AV1 spatial SVC.
`kind` defaults to the hidden constant `media`; the designer does not show a
kind selector. Chrome readiness requires configured resolution,
non-black pixels, and changing decoded frame hashes; native readiness requires
first media on selected track.

See [PLAYBACK_METRICS.md](PLAYBACK_METRICS.md) for native playback simulation,
the shared live metrics contract, and the Containernet visualizer data flow.

## Layout

```
moqlab/
├── requirements.txt                ← runtime deps (pydantic, click, pyyaml, docker)
├── requirements-dev.txt            ← + pytest, for the dev venv
├── pytest.ini                      ← pytest config (testpaths, addopts)
├── conftest.py                     ← empty; marks the pytest rootdir
├── README.md                       ← you are here
├── PLAYBACK_METRICS.md             ← playback simulation + live metrics report
├── AGENTS.md                       ← guide for AI agents touching this package
├── TODO.md                         ← deferred / future work
├── moqlab/                         ← package source (PEP 420 namespace pkg, no __init__.py)
│   ├── __main__.py                 ← `python -m moqlab` entry point
│   ├── build.py                    ← build command planning helpers
│   ├── cli.py                      ← Click commands
│   ├── exceptions.py
│   ├── visualizer.py               ← localhost visualizer API/static server
│   ├── trafficgen.py                ← custom bulk/CBR/segmented traffic runtime
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
│       ├── tree_3r_4s.yaml        ← 3-relay tree behind one core router + 4 subscribers
│       ├── external_traffic.yaml  ← two traffic containers across two named paths
│       ├── media_svc_metrics.yaml ← headless Chrome + simulated native subscriber
│       └── 100subs.yaml           ← 100 simulated native subscribers on one relay
├── docker/                         ← Dockerfiles for node images
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

# Builds moqlab-relay, moqlab-router, and moqlab-traffic from the repo root
# context. The router image compiles a pinned modern iproute2 so
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
                tls: { insecure: false, generated: true },
                cache: { enabled: false, max_tracks: 100, max_groups_per_track: 3 } }
  publisher:  { image: moqlab-media-pub }
  subscriber: { image: moqlab-media-sub,
                native_media_image: moqlab-media-native-sub,
                media_client: chrome-headless, log_level: INFO }

startup:
  relay_warmup_s: 2.0
  publisher_warmup_s: 1.0
  traffic_ready_timeout_s: 5.0

relays:
  relay-a: { listen_port: 9668, admin_port: 9669, upstream: null }
  relay-b: { listen_port: 9670, admin_port: 9671, upstream: relay-a }
  relay-c: { listen_port: 9672, admin_port: 9673, upstream: relay-b }

publishers:
  pub:     { connects_to: relay-a, namespace: moq-date }

subscribers:
  sub:     { connects_to: relay-c, namespace: moq-date, track: date }

routers:                     # Containernet only; Docker backend refuses
  rt-ab: { aqm: dualpi2 }    # AQM applies to every rt-ab egress
  rt-bc: { aqm: dualpi2 }

traffic:                     # optional; exactly one sender + one receiver
  sender: { id: traffic-tx }
  receiver: { id: traffic-rx }
  routes:
    west: { path: [traffic-tx, rt-ab, traffic-rx] }
  flows:
    - { id: bulk, kind: bulk, route: west, start_s: 0, duration_s: 30,
        connections: 2 }

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
    forward: { bandwidth_mbps: 50 }   # bottleneck on router egress
    reverse: { delay_ms: 10 }
  - { from: traffic-tx, to: rt-ab }
  - { from: rt-ab, to: traffic-rx }
  # ... rt-bc, relay-c, sub follow the same pattern
```

Set `l4s_ce_target: 0.05` on a relay to make its mvfst listener send ECT(1)
and react to CE feedback. Omit it to leave ECN disabled. This affects
connections accepted by that relay, such as relay-to-subscriber traffic.

Per direction (`forward` = from→to, `reverse` = to→from) you can set
`bandwidth_mbps` (HTB rate) and `delay_ms` / `jitter_ms` / `loss_pct`
(netem). Set `aqm` (currently `dualpi2`) on a router; it applies to every
egress interface owned by that router. See [ROUTER.md](ROUTER.md).

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
| `aqm` belongs to a router and applies to all its egress links | Keeps queue policy on the node that owns those interfaces. |
| Every declared router appears in at least one link | An unwired router is a config bug. |
| Traffic paths start at sender, end at receiver, use routers internally, and follow declared links | Makes selected routes explicit and reproducible. |
| `jitter_ms` requires `delay_ms` | netem expresses jitter as a variation of delay. |
| Unknown fields rejected | Surfaces typos as `ConfigError`. |

### External traffic

[`external_traffic.yaml`](configs/examples/external_traffic.yaml) runs one
`moqlab-traffic` sender and one receiver across multiple named paths. Backend
generates one sender/receiver `/32` alias pair per path and installs symmetric
hop-by-hop routes, so concurrent flows between same two containers can traverse
different routers. Generated aliases and resolved flows are saved as
`<run_dir>/traffic-plan.json`; configs never contain IP addresses.

Available flow kinds:

- `bulk`: continuous TCP writes over `connections` sockets.
- `cbr`: paced UDP using `rate_mbps` and `packet_size_bytes`.
- `segmented`: DASH-like TCP bursts. Each client sends one segment per
  `segment_duration_ms`; segment sizes cycle through
  `representation_sequence_mbps`.

Sender schedules every flow from one monotonic epoch. JSONL logs contain
planned flow identity, actual timings, byte/packet totals, and CBR late-packet
counts. `segmented` models network load only: no browser, MPD, codec, or media.
Python scheduling gives deterministic inputs, not bit-identical kernel timing.
ECN/L4S socket behavior is intentionally deferred.

### Topology designer

The local designer creates the same strict YAML consumed by `validate` and
`run`, without starting Docker or Containernet:

```bash
cd moqlab
python -m moqlab design
python -m moqlab design -c configs/examples/external_traffic.yaml
python -m moqlab design --port 8765
```

Open the printed localhost URL. Drag or click relays, routers, publishers,
subscribers, and traffic endpoints onto the canvas. Right-click a node, then
select another node to connect them with a physical link; the palette link
button remains available for keyboard-first use. Click a physical link on the
canvas to select it and edit its endpoints or directional shaping in the
right-side inspector. Configure application references and traffic loads there
too. Each load combines its path and traffic pattern in one place;
named reusable routes remain under Advanced routes. Bundled examples, bulk
node duplication, undo/redo, automatic layout, server-side validation, YAML
preview, and YAML download are built in. Draft data and node positions stay in
browser `localStorage`; positions never enter topology YAML.
New nodes receive concrete effective image, endpoint, TLS/cache, logging, and
media defaults. Service ports are allocated from the next free value and the
editor blocks export when manually entered ports collide.

Forms come from `TopologyConfig.model_json_schema()`, while a small manifest
describes graph-specific relationships. Schema coverage tests fail when a new
config construct lacks editor support. Imported YAML is exported in normalized
form: configuration meaning and explicit values remain, but comments and
original formatting do not. The designer cannot run experiments or write
repository files; use the downloaded file with `moqlab run`.

### AV1-SVC media nodes

See [`media_svc_headless.yaml`](configs/examples/media_svc_headless.yaml),
[`media_svc_x11.yaml`](configs/examples/media_svc_x11.yaml). Mixed native and
Chrome subscribers are shown in
[`media_svc_mixed.yaml`](configs/examples/media_svc_mixed.yaml); a headless
Chrome plus simulated-native metrics setup with lightly shaped subscriber paths is in
[`media_svc_metrics.yaml`](configs/examples/media_svc_metrics.yaml). Media
topologies generate one ECDSA P-256 certificate per run and mount it into all
relays plus the media origin. The root relay pulls `mlmpub`; Chromium trusts
the same certificate through the publisher's `/fingerprint` endpoint.
Relay-to-relay hops remain encrypted but currently use insecure upstream
verification because moqx does not yet implement `upstream.tls.ca_cert`.

```yaml
defaults:
  relay:
    tls: { insecure: false, generated: true }
  publisher: { image: moqlab-media-pub }
  subscriber:
    image: moqlab-media-sub
    native_media_image: moqlab-media-native-sub
    media_client: native
    native_playback: simulate

startup:
  media_ready_timeout_s: 30

publishers:
  pub:
    connects_to: relay-a
    asset: testsvc
    listen_port: 4443
    fingerprint_port: 8081

subscribers:
  sub:
    media_client: chrome-headless
    connects_to: relay-c
    namespace: msf/clear
    track: video/s2
    minimal_buffer_ms: 200
    target_latency_ms: 300
```

`media_client: chrome-headless` launches real WARP Player decode/playback in
headless Chromium. `media_client: chrome` runs the same real playback in a
visible Chrome window through X11.
`media_client: native` launches lightweight `mlmsub` over raw MoQT/QUIC,
subscribes to the configured track plus its catalog dependency chain, and
becomes ready after receiving its first media group. `native_playback` defaults
to `receive`; `simulate` models AV1-SVC decode-chain validity, buffering,
presentation, stalls, and safe layer switches without decoding pixels. Buffer
settings apply to both Chrome modes and simulated native playback; native
`receive` has no playback buffer controls. For large topologies,
set `defaults.subscriber.media_client: native` and
`defaults.subscriber.native_playback: simulate`, then override only subscribers
that need Chrome. [`100subs.yaml`](configs/examples/100subs.yaml) shows 100
simulated native subscribers connected to one relay. Schema default remains
`chrome-headless`, and native playback defaults to `receive`.

Every media subscriber publishes live metrics inside its container at
`/tmp/moqlab-player-metrics.json`. With `--visualize`, click a subscriber node
to inspect its state, quality, latency, bitrates, buffer, playback rate, and
stalls. The visualizer reads only the selected node once per second. Detailed
definitions and AV1-SVC switching rules are in
[PLAYBACK_METRICS.md](PLAYBACK_METRICS.md).

Moqlab starts all subscriber containers before checking media readiness, then
delays the publisher's one-shot catalog by two seconds. Mixed clients behind a
shared relay can all attach before that catalog is forwarded.

`media_client: chrome` opens Chromium directly on host X display. Backend mounts
`/tmp/.X11-unix` and forwards `DISPLAY`. Grant only root
local-display access, preserve `DISPLAY` through sudo, then revoke access.
`moqlab run` prints this reminder whenever the topology contains headed Chrome:

```bash
xhost +SI:localuser:root
sudo --preserve-env=DISPLAY ../../../containernet/venv/bin/python3 -m moqlab run \
  -c configs/examples/media_svc_x11.yaml --backend containernet --visualize
xhost -SI:localuser:root
```

Current scope is clear LOC AV1 spatial-only SVC, one temporal layer,
decode-availability switching in simulated native clients, manual Chrome
quality selection, and one media origin per relay tree. Namespace selection
comes from YAML because current relays do not replay earlier announcements.
The automation runner uses the player's catalog-subscription mode; it does not
depend on a joining `FETCH` being proxied by the relay.
There is no bandwidth-estimation ABR, DRM, audio selection, or temporal SVC.

## How wiring works

`moqlab run` synthesizes one moqx YAML per relay into
`<runs_dir>/<run_id>/configs/<relay>.yaml`, then:

- **Docker backend**: creates bridge network `moqlab_<run_id>`. Starts media
  origins first, relays root-first, then subscribers. The
  configured warmups separate those phases. Each container is named after its
  node id; URLs like `moqt://relay-a:9668/moq-relay` and
  `https://relay-c:9672/moq-relay` resolve via Docker DNS.
- **Containernet backend**: requires explicit `links:` wiring. Each link is
  one direct host↔host veth pair with its own /24 out of `10.20.0.0/16`
  (.1 = `from` side, .2 = `to` side); no switches, no controller. Long node
  ids get stable shortened veth names within Linux's 15-byte limit. Every node
  also gets a canonical /32 on `lo` out of `10.99.0.0/24`; `/etc/hosts` on
  every node maps all peer names to those /32s, and the backend installs
  static /32 routes (BFS over the link graph) so the same name-based URLs
  work across any number of router hops. Routers are plain Docker hosts with
  `net.ipv4.ip_forward=1` (plus `rp_filter=0` and ICMP-redirect suppression)
  that run no MoQ binary. After `net.start()` the backend assigns loopbacks,
  then installs route-specific traffic aliases when `traffic:` is present.
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
| `python -m moqlab design [-c <config>] [--port PORT]` | n/a | Open the dependency-free localhost drag-and-drop topology YAML designer. Validates and downloads configs; never runs a topology. |
| `python -m moqlab build moqx` | n/a | Build moqx and prepare moxygen binaries used by images. |
| `python -m moqlab build images` | n/a | Build `moqlab-relay`, `moqlab-router`, and `moqlab-traffic`. |
| `python -m moqlab build media-images [--publisher-context PATH] [--player-context PATH]` | n/a | Build `moqlab-media-pub`, `moqlab-media-sub`, and `moqlab-media-native-sub` from local contexts. Environment equivalents: `MOQLAB_MEDIA_PUBLISHER_CONTEXT` and `MOQLAB_MEDIA_PLAYER_CONTEXT`. |
| `python -m moqlab validate -c <config>` | both | Parse + validate, no side effects. |
| `python -m moqlab run -c <config> [--backend docker\|containernet] [--run-id N] [--publish-ports] [--vis\|--visualize]` | both | Run topology. Defaults to `containernet`. With `--visualize`, also serves `http://127.0.0.1:8765/` showing a pannable/zoomable topology graph and link rates. During a Containernet run, select a link to change its per-direction capacity, delay, jitter, or loss in place; queued packets are preserved, and hierarchy-changing edits are rejected. Select a router to change AQM on all its egress interfaces. Runtime edits do not rewrite topology YAML. Docker-backend runs remain read-only because their shared bridge has no per-topology-link interface. |
| `python -m moqlab down --run-id <name>` | docker | Stop and remove containers + network. |
| `python -m moqlab ls` | docker | List active runs. |
| `python -m moqlab logs --run-id <name> [-f] [-n N] <node_id>` | docker | Container logs for one node. |
| `python -m moqlab rm pycaches` | n/a | Remove project `__pycache__` dirs, `.pyc` / `.pyo` files, and `.pytest_cache`, skipping `.venv` and `.runs`. |

> **Note:** Live link editing requires the Containernet backend. Each
> Containernet link has its own veth pair, so the visualizer can change its
> capacity, delay, jitter, and loss. Router AQM is also editable there. Docker uses one shared bridge with no
> separate interface per topology link, so its visualizer is read-only.

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
collisions, link dedup, link-graph reachability, router-owned AQM,
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
