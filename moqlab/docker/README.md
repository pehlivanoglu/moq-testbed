# moqlab/docker — Image-build inputs

This directory contains the Dockerfiles for the node images the
orchestrator launches. **There is no orchestration here.** Topologies are
defined in YAML under [../configs/](../configs/) and brought up with
`moqlab run`.

| File | Image | Binary | Used by |
|---|---|---|---|
| `Dockerfile.relay` | `moqlab-relay` | `/usr/local/bin/moqx` | Every `relays:` entry |
| `Dockerfile.pub`   | `moqlab-pub`   | `/usr/local/bin/moqdateserver` | Every `publishers:` entry |
| `Dockerfile.sub`   | `moqlab-sub`   | `/usr/local/bin/moqtextclient` | Every `subscribers:` entry |
| `Dockerfile.router` | `moqlab-router` | none (IP forwarding + tc only) | Every `routers:` entry |
| `Dockerfile.media-pub` | `moqlab-media-pub` | `/usr/local/bin/mlmpub` | Media publishers |
| `Dockerfile.media-sub` | `moqlab-media-sub` | Chromium + WARP Player runner | Media subscribers |
| `Dockerfile.media-native-sub` | `moqlab-media-native-sub` | `/usr/local/bin/mlmsub` | Native media subscribers |

The router image runs no MoQ binary. It exists to own link queues: it builds
a pinned modern iproute2 from source (multi-stage) because distro tc is too
old to know L4S AQMs like `dualpi2`, and ships `ethtool`, `tcpdump`, and
`ping` for in-path debugging. The build fails if the compiled tc does not
recognize `dualpi2`.

The relay image expects its config bind-mounted at `/etc/moqx/relay.yaml`. The
orchestrator synthesizes that file from the topology config at run time.

The text and media pub/sub images take their arguments as the container `command`; the
orchestrator builds the argv from the topology config's
`publishers:` / `subscribers:` blocks.

## Building the images

The Dockerfiles `COPY` pre-built binaries from build-output paths in the repo
root (e.g. `build/moqx`, `.scratch/moxygen-install/bin/moqdateserver`).
Build the binaries and images through the moqlab CLI:

```bash
cd ../
python -m moqlab build moqx
python -m moqlab build images
python -m moqlab build media-images
```

`build images` runs Docker builds from the repository root context so the
Dockerfiles can copy `build/moqx` and `.scratch/moxygen-install/bin/...`
without requiring manual path juggling.

`build media-images` uses BuildKit named contexts instead of cloning or
vendoring. Its defaults are sibling `moqlivemock-svc` and `warp-player-svc`
repositories. Native subscriber image builds `mlmsub` from `moqlivemock-svc`.
Chrome subscriber runtime starts a Node static server, Chromium,
and the CDP readiness runner. X11 mode uses host display socket directly. No
process manager is used. Chrome and native media subscribers atomically write
live player metrics to `/tmp/moqlab-player-metrics.json`; simulated native
playback does not add a decoder or FFmpeg process.

Image tags can be overridden per topology via `defaults.relay.image` /
`defaults.publisher.image` / `defaults.subscriber.image` /
`defaults.router.image` or per-node `image:`. The router image needs no
moqx/moxygen artifacts and can be built standalone:
`docker build -f moqlab/docker/Dockerfile.router -t moqlab-router .`

## Why no docker-compose here

The pre-v1 `docker-compose.yml` was replaced by the `moqlab` CLI. Compose can
only express a single hard-coded service set; the CLI takes a topology YAML
and instantiates whatever it describes, including pubs, subs, and links.
