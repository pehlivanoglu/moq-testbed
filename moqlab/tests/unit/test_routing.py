"""Unit tests for orchestrator/routing.py next-hop and route rendering."""

import pytest

from moqlab.orchestrator.routing import next_hops, route_commands


def test_linear_chain_next_hops_both_directions():
    nodes = ["pub", "rt-1", "relay-a", "rt-2", "sub"]
    links = [("pub", "rt-1"), ("rt-1", "relay-a"), ("relay-a", "rt-2"), ("rt-2", "sub")]
    hops = next_hops(nodes, links)

    assert hops["pub"]["sub"] == "rt-1"
    assert hops["pub"]["relay-a"] == "rt-1"
    assert hops["sub"]["pub"] == "rt-2"
    assert hops["rt-1"]["sub"] == "relay-a"
    assert hops["rt-2"]["pub"] == "relay-a"
    # Direct neighbors route via each other.
    assert hops["pub"]["rt-1"] == "rt-1"


def test_star_router_fans_out():
    nodes = ["relay-1", "relay-2", "relay-3", "rt-core"]
    links = [("rt-core", "relay-1"), ("rt-core", "relay-2"), ("rt-core", "relay-3")]
    hops = next_hops(nodes, links)

    assert hops["relay-1"]["relay-2"] == "rt-core"
    assert hops["relay-1"]["relay-3"] == "rt-core"
    assert hops["rt-core"]["relay-2"] == "relay-2"


def test_diamond_equal_cost_is_deterministic():
    # a -> {m1, m2} -> z: both paths are two hops; sorted adjacency must make
    # the pick stable across runs.
    nodes = ["a", "m1", "m2", "z"]
    links = [("a", "m1"), ("a", "m2"), ("m1", "z"), ("m2", "z")]
    hops = next_hops(nodes, links)

    assert hops["a"]["z"] == "m1"
    assert hops["z"]["a"] == "m1"


def test_unreachable_nodes_absent():
    nodes = ["a", "b", "island"]
    links = [("a", "b")]
    hops = next_hops(nodes, links)

    assert "island" not in hops["a"]
    assert hops["island"] == {}


def test_link_referencing_unknown_node_rejected():
    with pytest.raises(ValueError, match="not in `nodes`"):
        next_hops(["a"], [("a", "ghost")])


def test_route_commands_render_via_dev_src():
    loopbacks = {"sub": "10.99.0.4", "relay-a": "10.99.0.1", "pub": "10.99.0.3"}
    hops = {"relay-a": "rt-1", "pub": "rt-1"}
    neighbor_addrs = {"rt-1": ("10.20.3.1", "sub-eth0")}

    cmds = route_commands("sub", hops, loopbacks, neighbor_addrs)
    assert cmds == [
        "ip route replace 10.99.0.3/32 via 10.20.3.1 dev sub-eth0 src 10.99.0.4",
        "ip route replace 10.99.0.1/32 via 10.20.3.1 dev sub-eth0 src 10.99.0.4",
    ]


def test_route_commands_reject_non_neighbor_next_hop():
    with pytest.raises(ValueError, match="not a direct neighbor"):
        route_commands(
            "sub",
            {"pub": "rt-9"},
            {"sub": "10.99.0.4", "pub": "10.99.0.3"},
            {"rt-1": ("10.20.3.1", "sub-eth0")},
        )
