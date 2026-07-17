"""Docker backend for the moqlab topology orchestrator.

One container per node on a single user-defined bridge network. Container name
equals the topology node id so moqx upstream URLs (`moqt://relay-a:9668/...`)
and pub/sub client URLs (`https://relay-a:9668/...`) resolve via Docker DNS.

State of truth lives in Docker labels:
  - moqlab.run_id     — the run identifier
  - moqlab.role       — relay | publisher | subscriber
  - moqlab.node_id    — the topology node id

Per-run scratch dir at <runs_dir>/<run_id>/ holds the input topology and the
synthesized relay YAMLs (mounted into the relay containers). It can be deleted
without stranding containers — `ls`/`down` re-derive state from labels.

The Docker backend silently ignores the `links:` block (no per-link shaping in
plain Docker) and refuses topologies that declare routers: flattening a
topology whose bottlenecks live on router nodes would run fine but measure
nothing. Use the Containernet backend for shaping and routed paths.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.models.networks import Network

from moqlab.config.schema import TopologyConfig, load_topology
from moqlab.config.synth import (
    synthesize_publisher_command,
    synthesize_relay_configs,
    synthesize_subscriber_command,
)
from moqlab.exceptions import (
    OrchestratorError,
    RunAlreadyExistsError,
    RunNotFoundError,
)
from moqlab.runtime import default_run_id, default_runs_dir, relay_order, validate_run_id

LABEL_RUN_ID = "moqlab.run_id"
LABEL_ROLE = "moqlab.role"
LABEL_NODE_ID = "moqlab.node_id"

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    network_id: str
    network_name: str
    run_dir: Path
    relays: dict[str, str] = field(default_factory=dict)
    publishers: dict[str, str] = field(default_factory=dict)
    subscribers: dict[str, str] = field(default_factory=dict)


class DockerBackend:
    def __init__(self, runs_dir: str | Path | None = None) -> None:
        self._runs_root = Path(runs_dir) if runs_dir else default_runs_dir()
        self._runs_root.mkdir(parents=True, exist_ok=True)
        try:
            self._client = docker.from_env()
            self._client.ping()
        except Exception as e:
            raise OrchestratorError(f"cannot reach Docker daemon: {e}") from e

    # ── public API ──────────────────────────────────────────────────────────

    def up(
        self,
        config_path: str | Path,
        run_id: str | None = None,
        publish_ports: bool = False,
        readiness_timeout_s: float = 10.0,
    ) -> RunRecord:
        topology = load_topology(config_path)
        if topology.routers:
            raise OrchestratorError(
                "topology declares routers; the docker backend is a flat "
                "bridge with no shaping or forwarding nodes, so running it "
                "would silently drop the declared bottlenecks — use "
                "`--backend containernet`, or remove the routers"
            )
        run_id = run_id or default_run_id()
        validate_run_id(run_id)

        if self._run_dir(run_id).exists() or self._find_network(run_id) is not None:
            raise RunAlreadyExistsError(
                f"run {run_id!r} already exists; use `moqlab down --run-id {run_id}` first"
            )

        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(config_path, run_dir / "topology.yaml")
        relay_yaml_paths = synthesize_relay_configs(topology, run_dir / "configs")

        common_labels = {LABEL_RUN_ID: run_id}
        network = self._create_network(run_id, common_labels)

        relays: dict[str, str] = {}
        publishers: dict[str, str] = {}
        subscribers: dict[str, str] = {}

        try:
            # Phase 1: relays, upstream-first so each one's upstream is up.
            for rid in relay_order(topology):
                container = self._run_relay(
                    topology=topology,
                    relay_id=rid,
                    config_path=relay_yaml_paths[rid],
                    network=network,
                    publish_ports=publish_ports,
                    labels=common_labels,
                )
                relays[rid] = container.id
                self._await_running(container, readiness_timeout_s)

            self._sleep_if_configured(
                topology.startup.relay_warmup_s,
                "after launching relays",
            )

            # Phase 2: publishers, then subscribers. The configured publisher
            # warmup lets PUBLISH_NAMESPACE reach relays before the first
            # subscriber asks for that namespace.
            for pid in topology.publishers:
                container = self._run_publisher(
                    topology=topology,
                    publisher_id=pid,
                    network=network,
                    labels=common_labels,
                )
                publishers[pid] = container.id
                self._await_running(container, readiness_timeout_s)

            if publishers and topology.subscribers:
                self._sleep_if_configured(
                    topology.startup.publisher_warmup_s,
                    "after launching publishers",
                )

            for sid in topology.subscribers:
                container = self._run_subscriber(
                    topology=topology,
                    subscriber_id=sid,
                    network=network,
                    labels=common_labels,
                )
                subscribers[sid] = container.id
                self._await_running(container, readiness_timeout_s)
        except Exception:
            self._teardown(run_id, swallow=True)
            raise

        return RunRecord(
            run_id=run_id,
            network_id=network.id,
            network_name=network.name,
            run_dir=run_dir,
            relays=relays,
            publishers=publishers,
            subscribers=subscribers,
        )

    def down(self, run_id: str) -> None:
        if self._find_network(run_id) is None and not self._run_dir(run_id).exists():
            raise RunNotFoundError(f"no run with id {run_id!r}")
        self._teardown(run_id, swallow=False)

    def ls(self) -> list[RunRecord]:
        nets = self._client.networks.list(filters={"label": LABEL_RUN_ID})
        out: list[RunRecord] = []
        for net in nets:
            run_id = net.attrs.get("Labels", {}).get(LABEL_RUN_ID)
            if not run_id:
                continue
            relays: dict[str, str] = {}
            publishers: dict[str, str] = {}
            subscribers: dict[str, str] = {}
            for c in self._running_containers_for(run_id):
                role = c.labels.get(LABEL_ROLE)
                node_id = c.labels.get(LABEL_NODE_ID, c.name)
                bucket = {
                    "relay": relays,
                    "publisher": publishers,
                    "subscriber": subscribers,
                }.get(role)
                if bucket is not None:
                    bucket[node_id] = c.id
            if not (relays or publishers or subscribers):
                continue
            out.append(
                RunRecord(
                    run_id=run_id,
                    network_id=net.id,
                    network_name=net.name,
                    run_dir=self._run_dir(run_id),
                    relays=relays,
                    publishers=publishers,
                    subscribers=subscribers,
                )
            )
        return out

    def container_for(self, run_id: str, node_id: str) -> Container:
        for c in self._containers_for(run_id):
            if c.labels.get(LABEL_NODE_ID) == node_id:
                return c
        raise RunNotFoundError(f"node {node_id!r} not found in run {run_id!r}")

    # ── internals ───────────────────────────────────────────────────────────

    def _run_dir(self, run_id: str) -> Path:
        return self._runs_root / run_id

    def _create_network(self, run_id: str, labels: dict[str, str]) -> Network:
        name = f"moqlab_{run_id}"
        try:
            return self._client.networks.create(
                name=name,
                driver="bridge",
                labels=labels,
            )
        except APIError as e:
            raise OrchestratorError(f"failed to create network {name!r}: {e}") from e

    def _find_network(self, run_id: str) -> Network | None:
        nets = self._client.networks.list(filters={"label": f"{LABEL_RUN_ID}={run_id}"})
        return nets[0] if nets else None

    def _containers_for(self, run_id: str) -> list[Container]:
        return self._client.containers.list(
            all=True,
            filters={"label": f"{LABEL_RUN_ID}={run_id}"},
        )

    def _running_containers_for(self, run_id: str) -> list[Container]:
        return self._client.containers.list(
            all=False,
            filters={"label": f"{LABEL_RUN_ID}={run_id}"},
        )

    def _run_relay(
        self,
        topology: TopologyConfig,
        relay_id: str,
        config_path: Path,
        network: Network,
        publish_ports: bool,
        labels: dict[str, str],
    ) -> Container:
        relay = topology.relays[relay_id]
        image = topology.relay_image(relay_id)

        ports: dict[str, int] | None = None
        if publish_ports:
            ports = {
                f"{relay.listen_port}/udp": relay.listen_port,
                f"{relay.admin_port}/tcp": relay.admin_port,
            }

        merged_labels = {
            **labels,
            LABEL_ROLE: "relay",
            LABEL_NODE_ID: relay_id,
        }

        return self._run_container(
            image=image,
            name=relay_id,
            network=network,
            labels=merged_labels,
            volumes={
                str(config_path.resolve()): {
                    "bind": "/etc/moqx/relay.yaml",
                    "mode": "ro",
                }
            },
            ports=ports,
        )

    def _run_publisher(
        self,
        topology: TopologyConfig,
        publisher_id: str,
        network: Network,
        labels: dict[str, str],
    ) -> Container:
        image = topology.publisher_image(publisher_id)
        argv = synthesize_publisher_command(topology, publisher_id)
        merged_labels = {
            **labels,
            LABEL_ROLE: "publisher",
            LABEL_NODE_ID: publisher_id,
        }
        return self._run_container(
            image=image,
            name=publisher_id,
            network=network,
            labels=merged_labels,
            command=argv,
        )

    def _run_subscriber(
        self,
        topology: TopologyConfig,
        subscriber_id: str,
        network: Network,
        labels: dict[str, str],
    ) -> Container:
        image = topology.subscriber_image(subscriber_id)
        argv = synthesize_subscriber_command(topology, subscriber_id)
        merged_labels = {
            **labels,
            LABEL_ROLE: "subscriber",
            LABEL_NODE_ID: subscriber_id,
        }
        return self._run_container(
            image=image,
            name=subscriber_id,
            network=network,
            labels=merged_labels,
            command=argv,
        )

    def _run_container(
        self,
        *,
        image: str,
        name: str,
        network: Network,
        labels: dict[str, str],
        volumes: dict | None = None,
        ports: dict | None = None,
        command: list[str] | None = None,
    ) -> Container:
        try:
            return self._client.containers.run(
                image=image,
                name=name,
                detach=True,
                network=network.name,
                hostname=name,
                labels=labels,
                volumes=volumes,
                ports=ports,
                command=command,
                restart_policy={"Name": "no"},
            )
        except ImageNotFound as e:
            raise OrchestratorError(
                f"image {image!r} not found locally; build images first "
                f"(see moqlab/docker/README.md)"
            ) from e
        except APIError as e:
            raise OrchestratorError(
                f"failed to start container {name!r}: {e}"
            ) from e

    def _await_running(self, container: Container, timeout_s: float) -> None:
        """Wait for the container to settle into `running` or fail loudly.

        We don't probe an application-level endpoint here — base images have
        no nc/curl. Crash-during-startup is detected by checking for an
        exited/dead status within `timeout_s`.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            container.reload()
            status = container.status
            if status == "running":
                return
            if status in {"exited", "dead"}:
                logs = container.logs(tail=50).decode("utf-8", errors="replace")
                raise OrchestratorError(
                    f"container {container.name!r} exited during startup; "
                    f"last logs:\n{logs}"
                )
            time.sleep(0.2)
        raise OrchestratorError(
            f"container {container.name!r} did not become running within {timeout_s}s"
        )

    @staticmethod
    def _sleep_if_configured(seconds: float, reason: str) -> None:
        if seconds <= 0:
            return
        _log.info("waiting %.3fs %s", seconds, reason)
        time.sleep(seconds)

    def _teardown(self, run_id: str, swallow: bool) -> None:
        for container in self._containers_for(run_id):
            try:
                container.remove(force=True)
            except (APIError, NotFound) as e:
                if not swallow:
                    raise OrchestratorError(
                        f"failed to remove container {container.name!r}: {e}"
                    ) from e
        net = self._find_network(run_id)
        if net is not None:
            try:
                net.remove()
            except (APIError, NotFound) as e:
                if not swallow:
                    raise OrchestratorError(
                        f"failed to remove network {net.name!r}: {e}"
                    ) from e
