"""Pure static-route computation over the topology link graph.

Every IP node gets a canonical /32 loopback address; the backend installs one
explicit /32 route per (node, destination) pair instead of default routes.
Uniform across endpoints and routers, and immune to Docker's own default
route on docker0.
"""

from __future__ import annotations

import ipaddress
from collections import deque
from typing import Iterable

from moqlab.config.schema import TopologyConfig
from moqlab.exceptions import OrchestratorError
from moqlab.runtime import containernet_edge_interfaces


def addressed_links(
    topology: TopologyConfig, pool: str = "10.20.0.0/16"
) -> tuple[dict[str, list[tuple[str, str]]], dict[str, dict[str, tuple[str, str]]]]:
    """Assign one subnet per direct link or switched LAN and resolve L3 peers.

    Bridge ports carry no IP. LAN members become routing neighbors across
    the bridge, using their own attachment interfaces for every LAN peer.
    """
    edges = containernet_edge_interfaces(topology)
    parent = list(range(len(edges)))

    def root(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i

    switch_edge: dict[str, int] = {}
    for i, edge in enumerate(edges):
        for node in (edge.a, edge.b):
            if node in topology.switches:
                if node in switch_edge:
                    parent[root(i)] = root(switch_edge[node])
                switch_edge[node] = i
    groups: dict[int, list[tuple[str, str]]] = {}
    interfaces: dict[str, list[tuple[str, str]]] = {}
    for i, edge in enumerate(edges):
        members = groups.setdefault(root(i), [])
        for node, iface in ((edge.a, edge.a_iface), (edge.b, edge.b_iface)):
            if node in topology.switches:
                interfaces.setdefault(node, []).append((iface, ""))
            else:
                members.append((node, iface))
    subnets = list(ipaddress.ip_network(pool).subnets(new_prefix=24))
    if len(groups) > len(subnets):
        raise OrchestratorError("link subnet pool exhausted")
    neighbors: dict[str, dict[str, tuple[str, str]]] = {}
    for subnet, members in zip(subnets, groups.values()):
        if len(members) > 254:
            raise OrchestratorError("switched LAN exceeds 254 addressed interfaces")
        addresses = [str(subnet.network_address + i) for i in range(1, len(members) + 1)]
        for (node, iface), address in zip(members, addresses):
            interfaces.setdefault(node, []).append((iface, f"{address}/24"))
            for (peer, _), peer_address in zip(members, addresses):
                if peer != node:
                    neighbors.setdefault(node, {})[peer] = (peer_address, iface)
    return interfaces, neighbors


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


def routed_path(path: Iterable[str], switches: Iterable[str]) -> list[str]:
    """Remove transparent Layer 2 bridge hops from a physical route path."""
    switch_ids = set(switches)
    return [node for node in path if node not in switch_ids]


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
