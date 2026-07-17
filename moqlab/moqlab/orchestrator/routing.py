"""Pure static-route computation over the topology link graph.

Every node gets a canonical /32 loopback address; the backend installs one
explicit /32 route per (node, destination) pair instead of default routes.
Uniform across endpoints and routers, and immune to Docker's own default
route on docker0.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable


def next_hops(
    nodes: Iterable[str], links: Iterable[tuple[str, str]]
) -> dict[str, dict[str, str]]:
    """Shortest-path next hops: node -> {destination -> neighbor to forward via}.

    BFS from each destination over the undirected link graph. Adjacency lists
    are sorted, so equal-cost ties always resolve the same way. Unreachable
    pairs are simply absent from the inner dict.
    """
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in links:
        if a not in adj or b not in adj:
            raise ValueError(f"link ({a!r}, {b!r}) references a node not in `nodes`")
        adj[a].append(b)
        adj[b].append(a)
    for n in adj:
        adj[n] = sorted(set(adj[n]))

    hops: dict[str, dict[str, str]] = {n: {} for n in adj}
    for dst in sorted(adj):
        # BFS rooted at the destination: each discovered node's BFS parent is
        # its next hop toward dst.
        parent: dict[str, str] = {}
        seen = {dst}
        queue = deque([dst])
        while queue:
            cur = queue.popleft()
            for neighbor in adj[cur]:
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                parent[neighbor] = cur
                queue.append(neighbor)
        for node, via in parent.items():
            hops[node][dst] = via
    return hops


def route_commands(
    node: str,
    hops: dict[str, str],
    loopback_ips: dict[str, str],
    neighbor_addrs: dict[str, tuple[str, str]],
) -> list[str]:
    """Render `ip route` commands for one node.

    hops: destination -> next-hop neighbor, i.e. next_hops(...)[node].
    loopback_ips: node id -> canonical loopback address (no prefix).
    neighbor_addrs: direct neighbor -> (neighbor's IP on the shared link,
    local interface facing it).

    `src` pins locally generated traffic to the node's canonical /32 so flows
    are addressed symmetrically no matter which interface they leave through.
    """
    own = loopback_ips[node]
    cmds: list[str] = []
    for dst in sorted(hops):
        via = hops[dst]
        if via not in neighbor_addrs:
            raise ValueError(
                f"next hop {via!r} for {node!r}->{dst!r} is not a direct neighbor"
            )
        via_ip, iface = neighbor_addrs[via]
        cmds.append(
            f"ip route replace {loopback_ips[dst]}/32 via {via_ip} dev {iface} src {own}"
        )
    return cmds
