from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

from moqlab.config.schema import DirectionSpec, TopologyConfig, load_topology
from moqlab.runtime import (
    containernet_edge_interfaces,
    relay_depths,
    relay_order,
    topology_edges,
)

_log = logging.getLogger(__name__)
_STATIC_ROOT = Path(__file__).resolve().parents[1] / "visualizer"
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


@dataclass(frozen=True)
class InterfaceCounters:
    rx_bytes: int
    tx_bytes: int


@dataclass(frozen=True)
class _EdgeCounterSample:
    at_s: float
    a: InterfaceCounters
    b: InterfaceCounters


CounterReader = Callable[[str, str], InterfaceCounters | None]
MetricsReader = Callable[[str], bytes | None]
_METRICS_PATH = "/tmp/moqlab-player-metrics.json"
_METRICS_STALE_MS = 3000


def _direction_payload(spec: DirectionSpec) -> dict[str, object]:
    return {
        "bandwidth_mbps": spec.bandwidth_mbps,
        "delay_ms": spec.delay_ms,
        "jitter_ms": spec.jitter_ms,
        "loss_pct": spec.loss_pct,
        "aqm": spec.aqm.value if spec.aqm is not None else None,
    }


def _link_graph_levels(topology: TopologyConfig) -> dict[str, int]:
    """BFS hop counts over the link graph, rooted at publishers (or relays).

    Used to place routers and routed endpoints in left-to-right columns; nodes
    in a disconnected component default to level 0.
    """
    adjacency: dict[str, set[str]] = {}
    for link in topology.links:
        adjacency.setdefault(link.from_, set()).add(link.to)
        adjacency.setdefault(link.to, set()).add(link.from_)

    sources = sorted(topology.publishers) or sorted(topology.relays)
    levels = {nid: 0 for nid in sources}
    queue = deque(sources)
    while queue:
        cur = queue.popleft()
        for neighbor in sorted(adjacency.get(cur, ())):
            if neighbor not in levels:
                levels[neighbor] = levels[cur] + 1
                queue.append(neighbor)
    return levels


def topology_snapshot(topology: TopologyConfig) -> dict[str, object]:
    nodes: list[dict[str, object]] = []
    depths = relay_depths(topology)
    link_levels = _link_graph_levels(topology) if topology.links else {}

    def _level(nid: str, fallback: int) -> int:
        return link_levels.get(nid, fallback)

    for rid in relay_order(topology):
        relay = topology.relays[rid]
        nodes.append(
            {
                "id": rid,
                "role": "relay",
                "level": _level(rid, depths[rid] + 1),
                "listen_port": relay.listen_port,
                "admin_port": relay.admin_port,
                "upstream": relay.upstream,
            }
        )
    for rid in topology.routers:
        nodes.append(
            {
                "id": rid,
                "role": "router",
                "level": _level(rid, 1),
            }
        )
    for pid, publisher in topology.publishers.items():
        nodes.append(
            {
                "id": pid,
                "role": "publisher",
                "level": _level(pid, depths[publisher.connects_to]),
                "connects_to": publisher.connects_to,
                "kind": publisher.kind,
                "namespace": publisher.namespace,
                "asset": publisher.asset,
            }
        )
    for sid, subscriber in topology.subscribers.items():
        nodes.append(
            {
                "id": sid,
                "role": "subscriber",
                "level": _level(sid, depths[subscriber.connects_to] + 2),
                "connects_to": subscriber.connects_to,
                "kind": subscriber.kind,
                "namespace": subscriber.namespace,
                "track": subscriber.track,
                "media_client": (
                    topology.subscriber_media_client(sid)
                    if subscriber.kind == "media"
                    else None
                ),
                "native_playback": (
                    topology.subscriber_native_playback(sid)
                    if subscriber.kind == "media"
                    and topology.subscriber_media_client(sid) == "native"
                    else None
                ),
                "browser_mode": subscriber.browser_mode,
            }
        )
    if topology.traffic is not None:
        for role, endpoint in (
            ("traffic-sender", topology.traffic.sender),
            ("traffic-receiver", topology.traffic.receiver),
        ):
            nodes.append(
                {
                    "id": endpoint.id,
                    "role": role,
                    "level": _level(endpoint.id, 0),
                }
            )

    min_level = min((int(node["level"]) for node in nodes), default=0)
    for node in nodes:
        node["level"] = int(node["level"]) - min_level

    links: list[dict[str, object]] = []
    if topology.links:
        for link in topology.links:
            links.append(
                {
                    "id": f"{link.from_}--{link.to}",
                    "source": link.from_,
                    "target": link.to,
                    "forward": _direction_payload(link.forward),
                    "reverse": _direction_payload(link.reverse),
                }
            )
    else:
        # No physical wiring declared (Docker backend): show the application
        # edges so the graph still renders.
        for a, b in topology_edges(topology):
            links.append(
                {
                    "id": f"{a}--{b}",
                    "source": a,
                    "target": b,
                    "forward": None,
                    "reverse": None,
                }
            )

    return {
        "nodes": nodes,
        "links": links,
        "summary": {
            "relays": len(topology.relays),
            "routers": len(topology.routers),
            "publishers": len(topology.publishers),
            "subscribers": len(topology.subscribers),
            "links": len(links),
            **({"traffic_endpoints": 2} if topology.traffic is not None else {}),
        },
    }


class ThroughputSampler:
    def __init__(
        self,
        topology: TopologyConfig,
        counter_reader: CounterReader | None = None,
    ) -> None:
        self._edges = containernet_edge_interfaces(topology)
        self._counter_reader = counter_reader or _read_containernet_counters
        self._previous: dict[str, _EdgeCounterSample] = {}

    def sample(self) -> dict[str, dict[str, object]]:
        now = time.monotonic()
        out: dict[str, dict[str, object]] = {}

        for edge in self._edges:
            edge_id = f"{edge.a}--{edge.b}"
            a = self._counter_reader(edge.a, edge.a_iface)
            b = self._counter_reader(edge.b, edge.b_iface)
            if a is None or b is None:
                self._previous.pop(edge_id, None)
                out[edge_id] = {
                    "status": "unavailable",
                    "throughput_bps": None,
                    "a_to_b_bps": None,
                    "b_to_a_bps": None,
                }
                continue

            current = _EdgeCounterSample(at_s=now, a=a, b=b)
            previous = self._previous.get(edge_id)
            self._previous[edge_id] = current
            if previous is None or now <= previous.at_s:
                out[edge_id] = {
                    "status": "warming",
                    "throughput_bps": None,
                    "a_to_b_bps": None,
                    "b_to_a_bps": None,
                }
                continue

            elapsed = now - previous.at_s
            a_to_b_bps = _bytes_per_second(previous.a.tx_bytes, a.tx_bytes, elapsed) * 8
            b_to_a_bps = _bytes_per_second(previous.b.tx_bytes, b.tx_bytes, elapsed) * 8
            out[edge_id] = {
                "status": "ok",
                "throughput_bps": a_to_b_bps + b_to_a_bps,
                "a_to_b_bps": a_to_b_bps,
                "b_to_a_bps": b_to_a_bps,
            }

        return out


def snapshot_with_rates(
    topology: TopologyConfig,
    sampler: ThroughputSampler,
) -> dict[str, object]:
    snapshot = topology_snapshot(topology)
    rates = sampler.sample()
    for link in snapshot["links"]:  # type: ignore[index]
        rate = rates.get(link["id"], {})  # type: ignore[index]
        link.update(rate)  # type: ignore[union-attr]
    snapshot["sampled_at_unix_s"] = time.time()
    return snapshot


class VisualizerHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        topology: TopologyConfig,
        backend: str = "containernet",
        metrics_reader: MetricsReader | None = None,
    ) -> None:
        super().__init__(server_address, _VisualizerHandler)
        self.topology = topology
        self.sampler = ThroughputSampler(topology)
        self.backend = backend
        self.metrics_reader = metrics_reader or _read_container_metrics
        self.subscriber_containers: dict[str, str] = {}

    def register_subscriber_containers(self, containers: dict[str, str]) -> None:
        self.subscriber_containers = dict(containers)

    def node_metrics(self, node_id: str) -> dict[str, object]:
        subscriber = self.topology.subscribers.get(node_id)
        if subscriber is None:
            return {"status": "unavailable", "reason": "unknown node"}
        if subscriber.kind != "media":
            return {"status": "unavailable", "reason": "player metrics unavailable"}
        container = (
            f"mn.{node_id}"
            if self.backend == "containernet"
            else self.subscriber_containers.get(node_id)
        )
        if not container:
            return {"status": "unavailable", "reason": "container not registered"}
        return parse_node_metrics(self.metrics_reader(container))


def make_server(
    *,
    config_path: str | Path,
    host: str,
    port: int,
    backend: str = "containernet",
    metrics_reader: MetricsReader | None = None,
) -> VisualizerHTTPServer:
    topology = load_topology(config_path)
    return VisualizerHTTPServer(
        (host, port), topology, backend=backend, metrics_reader=metrics_reader
    )


def parse_node_metrics(
    raw: bytes | None, *, now_unix_ms: int | None = None
) -> dict[str, object]:
    if raw is None:
        return {"status": "unavailable", "reason": "metrics not ready"}
    try:
        metrics = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "unavailable", "reason": "invalid metrics JSON"}
    if not isinstance(metrics, dict) or metrics.get("schema_version") != 1:
        return {"status": "unavailable", "reason": "unsupported metrics schema"}
    sampled_at = metrics.get("sampled_at_unix_ms")
    if not isinstance(sampled_at, (int, float)):
        return {"status": "unavailable", "reason": "missing metrics timestamp"}
    now = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    status = "stale" if now - sampled_at > _METRICS_STALE_MS else "ok"
    return {"status": status, "metrics": metrics}


class _VisualizerHandler(BaseHTTPRequestHandler):
    server: VisualizerHTTPServer

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/snapshot":
            self._send_json(snapshot_with_rates(self.server.topology, self.server.sampler))
            return
        prefix = "/api/nodes/"
        suffix = "/metrics"
        if path.startswith(prefix) and path.endswith(suffix):
            node_id = unquote(path[len(prefix):-len(suffix)]).strip("/")
            self._send_json(self.server.node_metrics(node_id))
            return
        static_file = _STATIC_FILES.get(path)
        if static_file is not None:
            self._send_static(*static_file)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: object) -> None:
        _log.debug("visualizer: " + fmt, *args)

    def _send_json(self, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_response_body(HTTPStatus.OK, "application/json", body)

    def _send_static(self, filename: str, content_type: str) -> None:
        path = _STATIC_ROOT / filename
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_response_body(HTTPStatus.OK, content_type, body)

    def _send_response_body(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            _log.debug("visualizer client disconnected before response completed")


def _bytes_per_second(previous: int, current: int, elapsed_s: float) -> float:
    return max(0, current - previous) / elapsed_s


def _read_containernet_counters(node_id: str, iface: str) -> InterfaceCounters | None:
    try:
        import docker
        from docker.errors import DockerException, NotFound
    except ImportError:
        return None

    try:
        client = docker.from_env()
        container = client.containers.get(f"mn.{node_id}")
        result = container.exec_run(
            [
                "cat",
                f"/sys/class/net/{iface}/statistics/rx_bytes",
                f"/sys/class/net/{iface}/statistics/tx_bytes",
            ]
        )
    except (DockerException, NotFound):
        return None

    if result.exit_code != 0:
        return None
    lines = result.output.decode("utf-8", errors="replace").splitlines()
    if len(lines) < 2:
        return None
    try:
        return InterfaceCounters(rx_bytes=int(lines[0]), tx_bytes=int(lines[1]))
    except ValueError:
        return None


def _read_container_metrics(container_id: str) -> bytes | None:
    try:
        import docker
        from docker.errors import DockerException, NotFound
    except ImportError:
        return None

    try:
        client = docker.from_env()
        result = client.containers.get(container_id).exec_run(["cat", _METRICS_PATH])
    except (DockerException, NotFound):
        return None
    if result.exit_code != 0 or len(result.output) > 64 * 1024:
        return None
    return bytes(result.output)
