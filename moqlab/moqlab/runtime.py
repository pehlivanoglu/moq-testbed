from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from moqlab.config.schema import TopologyConfig
from moqlab.exceptions import OrchestratorError

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}$")


@dataclass(frozen=True)
class ContainernetEdgeInterfaces:
    a: str
    b: str
    a_iface: str
    b_iface: str


def default_runs_dir() -> Path:
    return Path(__file__).resolve().parents[1] / ".runs"


def default_run_id() -> str:
    return time.strftime("run_%Y%m%d_%H%M%S")


def validate_run_id(run_id: str) -> None:
    """Raise OrchestratorError if run_id would escape the runs directory or break Docker naming."""
    if not _RUN_ID_RE.match(run_id):
        raise OrchestratorError(
            f"invalid run id {run_id!r}: must match {_RUN_ID_RE.pattern}"
        )


def relay_order(topology: TopologyConfig) -> list[str]:
    depth: dict[str, int] = {}

    def _depth(rid: str) -> int:
        if rid in depth:
            return depth[rid]
        upstream = topology.relays[rid].upstream
        depth[rid] = 0 if upstream is None else _depth(upstream) + 1
        return depth[rid]

    for rid in topology.relays:
        _depth(rid)
    return sorted(topology.relays.keys(), key=lambda rid: (depth[rid], rid))


def topology_edges(topology: TopologyConfig) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []

    def _add(a: str, b: str) -> None:
        key = tuple(sorted([a, b]))
        if key not in seen:
            seen.add(key)
            ordered.append(key)  # type: ignore[arg-type]

    for rid, relay in topology.relays.items():
        if relay.upstream is not None:
            _add(rid, relay.upstream)
    for pid, publisher in topology.publishers.items():
        _add(pid, publisher.connects_to)
    for sid, subscriber in topology.subscribers.items():
        _add(sid, subscriber.connects_to)
    return ordered


def containernet_edge_interfaces(
    topology: TopologyConfig,
) -> list[ContainernetEdgeInterfaces]:
    link_counter: dict[str, int] = {
        **{rid: 0 for rid in topology.relays},
        **{pid: 0 for pid in topology.publishers},
        **{sid: 0 for sid in topology.subscribers},
    }
    edges: list[ContainernetEdgeInterfaces] = []
    for a, b in topology_edges(topology):
        a_iface = f"{a}-eth{link_counter[a]}"
        link_counter[a] += 1
        b_iface = f"{b}-eth{link_counter[b]}"
        link_counter[b] += 1
        edges.append(
            ContainernetEdgeInterfaces(a=a, b=b, a_iface=a_iface, b_iface=b_iface)
        )
    return edges


def topology_image_tags(topology: TopologyConfig) -> set[str]:
    return {
        *(topology.relay_image(rid) for rid in topology.relays),
        *(topology.publisher_image(pid) for pid in topology.publishers),
        *(topology.subscriber_image(sid) for sid in topology.subscribers),
    }
