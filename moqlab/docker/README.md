# moqlab/docker — Image-build inputs

This directory contains the Dockerfiles for the three node images the
orchestrator launches. **There is no orchestration here.** Topologies are
defined in YAML under [../configs/](../configs/) and brought up with
`moqlab up`.

| File | Image | Binary | Used by |
|---|---|---|---|
| `Dockerfile.relay` | `moqlab-relay` | `/usr/local/bin/moqx` | Every `relays:` entry |
| `Dockerfile.pub`   | `moqlab-pub`   | `/usr/local/bin/moqdateserver` | Every `publishers:` entry |
| `Dockerfile.sub`   | `moqlab-sub`   | `/usr/local/bin/moqtextclient` | Every `subscribers:` entry |

The relay image expects its config bind-mounted at `/etc/moqx/relay.yaml`. The
orchestrator synthesizes that file from the topology config at run time.

The pub and sub images take their arguments as the container `command`; the
orchestrator builds the argv from the topology config's
`publishers:` / `subscribers:` blocks.

## Building the images

The Dockerfiles `COPY` pre-built binaries from build-output paths in the repo
root (e.g. `build/moqx`, `.scratch/moxygen-install/bin/moqdateserver`). Build
those first, then:

```bash
docker build -f moqlab/docker/Dockerfile.relay -t moqlab-relay ../..
docker build -f moqlab/docker/Dockerfile.pub   -t moqlab-pub   ../..
docker build -f moqlab/docker/Dockerfile.sub   -t moqlab-sub   ../..
```

Image tags can be overridden per topology via `defaults.relay.image` /
`defaults.publisher.image` / `defaults.subscriber.image` or per-node `image:`.

## Why no docker-compose here

The pre-v1 `docker-compose.yml` was replaced by the `moqlab` CLI. Compose can
only express a single hard-coded service set; the CLI takes a topology YAML
and instantiates whatever it describes, including pubs, subs, and links.
