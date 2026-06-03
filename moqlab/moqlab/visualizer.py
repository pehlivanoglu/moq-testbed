from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from moqlab.config.schema import TopologyConfig, load_topology
from moqlab.runtime import relay_order, topology_edges

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
class _EdgeInterfaces:
    a: str
    b: str
    a_iface: str
    b_iface: str


@dataclass(frozen=True)
class _EdgeCounterSample:
    at_s: float
    a: InterfaceCounters
    b: InterfaceCounters


CounterReader = Callable[[str, str], InterfaceCounters | None]


def topology_snapshot(topology: TopologyConfig) -> dict[str, object]:
    nodes: list[dict[str, object]] = []
    relay_depths = _relay_depths(topology)

    for rid in relay_order(topology):
        relay = topology.relays[rid]
        nodes.append(
            {
                "id": rid,
                "role": "relay",
                "level": relay_depths[rid] + 1,
                "listen_port": relay.listen_port,
                "admin_port": relay.admin_port,
                "upstream": relay.upstream,
            }
        )
    for pid, publisher in topology.publishers.items():
        nodes.append(
            {
                "id": pid,
                "role": "publisher",
                "level": relay_depths[publisher.connects_to],
                "connects_to": publisher.connects_to,
                "namespace": publisher.namespace,
            }
        )
    for sid, subscriber in topology.subscribers.items():
        nodes.append(
            {
                "id": sid,
                "role": "subscriber",
                "level": relay_depths[subscriber.connects_to] + 2,
                "connects_to": subscriber.connects_to,
                "namespace": subscriber.namespace,
                "track": subscriber.track,
            }
        )

    min_level = min((int(node["level"]) for node in nodes), default=0)
    for node in nodes:
        node["level"] = int(node["level"]) - min_level

    links: list[dict[str, object]] = []
    for a, b in topology_edges(topology):
        spec = topology.link_for(a, b)
        links.append(
            {
                "id": f"{a}--{b}",
                "source": a,
                "target": b,
                "bandwidth_mbps": spec.bandwidth_mbps if spec else None,
                "delay_ms": spec.delay_ms if spec else None,
                "jitter_ms": spec.jitter_ms if spec else None,
                "loss_pct": spec.loss_pct if spec else None,
            }
        )

    return {
        "nodes": nodes,
        "links": links,
        "summary": {
            "relays": len(topology.relays),
            "publishers": len(topology.publishers),
            "subscribers": len(topology.subscribers),
            "links": len(links),
        },
    }


class ThroughputSampler:
    def __init__(
        self,
        topology: TopologyConfig,
        counter_reader: CounterReader | None = None,
    ) -> None:
        self._edges = _containernet_edge_interfaces(topology)
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
    ) -> None:
        super().__init__(server_address, _VisualizerHandler)
        self.topology = topology
        self.sampler = ThroughputSampler(topology)


def make_server(
    *,
    config_path: str | Path,
    host: str,
    port: int,
) -> VisualizerHTTPServer:
    topology = load_topology(config_path)
    return VisualizerHTTPServer((host, port), topology)


class _VisualizerHandler(BaseHTTPRequestHandler):
    server: VisualizerHTTPServer

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/snapshot":
            self._send_json(snapshot_with_rates(self.server.topology, self.server.sampler))
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


def _relay_depths(topology: TopologyConfig) -> dict[str, int]:
    depths: dict[str, int] = {}

    def depth(rid: str) -> int:
        if rid in depths:
            return depths[rid]
        upstream = topology.relays[rid].upstream
        depths[rid] = 0 if upstream is None else depth(upstream) + 1
        return depths[rid]

    for rid in topology.relays:
        depth(rid)
    return depths


def _containernet_edge_interfaces(topology: TopologyConfig) -> list[_EdgeInterfaces]:
    link_counter: dict[str, int] = {
        **{rid: 0 for rid in topology.relays},
        **{pid: 0 for pid in topology.publishers},
        **{sid: 0 for sid in topology.subscribers},
    }
    edges: list[_EdgeInterfaces] = []
    for a, b in topology_edges(topology):
        a_iface = f"{a}-eth{link_counter[a]}"
        link_counter[a] += 1
        b_iface = f"{b}-eth{link_counter[b]}"
        link_counter[b] += 1
        edges.append(_EdgeInterfaces(a=a, b=b, a_iface=a_iface, b_iface=b_iface))
    return edges


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
