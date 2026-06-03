from __future__ import annotations

import time
from pathlib import Path

from moqlab.config.schema import TopologyConfig


def default_runs_dir() -> Path:
    return Path(__file__).resolve().parents[1] / ".runs"


def default_run_id() -> str:
    return time.strftime("run_%Y%m%d_%H%M%S")


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


def topology_image_tags(topology: TopologyConfig) -> set[str]:
    return {
        *(topology.relay_image(rid) for rid in topology.relays),
        *(topology.publisher_image(pid) for pid in topology.publishers),
        *(topology.subscriber_image(sid) for sid in topology.subscribers),
    }
