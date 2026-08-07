from __future__ import annotations

import hashlib
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


def relay_depths(topology: TopologyConfig) -> dict[str, int]:
    depths: dict[str, int] = {}

    def _depth(rid: str) -> int:
        if rid in depths:
            return depths[rid]
        upstream = topology.relays[rid].upstream
        depths[rid] = 0 if upstream is None else _depth(upstream) + 1
        return depths[rid]

    for rid in topology.relays:
        _depth(rid)
    return depths


def relay_order(topology: TopologyConfig) -> list[str]:
    depths = relay_depths(topology)
    return sorted(topology.relays.keys(), key=lambda rid: (depths[rid], rid))


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


def all_node_ids(topology: TopologyConfig) -> list[str]:
    """Every node id in declaration order: relays, routers, publishers, subscribers."""
    return [
        *topology.relays,
        *topology.routers,
        *topology.publishers,
        *topology.subscribers,
    ]


def containernet_edge_interfaces(
    topology: TopologyConfig,
) -> list[ContainernetEdgeInterfaces]:
    """One veth pair per `links:` entry, in declaration order.

    Direction is preserved (a = link.from_, b = link.to) so callers can map
    `forward`/`reverse` shaping onto a_iface/b_iface. Short node ids use
    Containernet's "<node>-eth<N>" scheme. Long ids get stable hashed names
    fitting Linux's 15-byte interface-name limit.
    """
    link_counter: dict[str, int] = {nid: 0 for nid in all_node_ids(topology)}
    edges: list[ContainernetEdgeInterfaces] = []
    for link in topology.links:
        a, b = link.from_, link.to
        a_iface = _containernet_interface_name(a, link_counter[a])
        link_counter[a] += 1
        b_iface = _containernet_interface_name(b, link_counter[b])
        link_counter[b] += 1
        edges.append(
            ContainernetEdgeInterfaces(a=a, b=b, a_iface=a_iface, b_iface=b_iface)
        )
    return edges


def _containernet_interface_name(node_id: str, index: int) -> str:
    natural = f"{node_id}-eth{index}"
    if len(natural) <= 15:
        return natural
    digest = hashlib.blake2s(node_id.encode(), digest_size=3).hexdigest()
    return f"{node_id[:4]}-{digest}e{index}"


# Canonical per-node addresses live on lo so they are reachable over any
# path; /24 pool → at most 254 nodes.
_LOOPBACK_POOL_PREFIX = "10.99.0."


def node_loopback_ips(topology: TopologyConfig) -> dict[str, str]:
    """Canonical /32 loopback address per node, deterministic by declaration order."""
    ordered = all_node_ids(topology)
    if len(ordered) > 254:
        raise OrchestratorError(
            f"topology has {len(ordered)} nodes but the loopback pool "
            f"{_LOOPBACK_POOL_PREFIX}0/24 only provides 254 addresses"
        )
    return {
        nid: f"{_LOOPBACK_POOL_PREFIX}{i}" for i, nid in enumerate(ordered, start=1)
    }


def topology_image_tags(topology: TopologyConfig) -> set[str]:
    return {
        *(topology.relay_image(rid) for rid in topology.relays),
        *(topology.publisher_image(pid) for pid in topology.publishers),
        *(topology.subscriber_image(sid) for sid in topology.subscribers),
        *(topology.router_image(rid) for rid in topology.routers),
    }
