from __future__ import annotations

from pathlib import Path

import pytest

from moqlab.config.schema import TopologyConfig
from moqlab.designer import schema_contract_issues
from moqlab.orchestrator.containernet_backend import ContainernetBackend, ContainernetRunRecord
from moqlab.orchestrator.routing import addressed_links, next_hops, route_commands
from moqlab.runtime import all_node_ids, node_loopback_ips, topology_image_tags
from moqlab.visualizer import topology_snapshot
from tests.unit.test_containernet_backend import _FakeBuildNet, _FakeNet


def topology_data() -> dict:
    return {
        "relays": {"relay": {"listen_port": 9668, "admin_port": 9669}},
        "routers": {"router": {"aqm": "dualpi2"}},
        "switches": {"sw": {}},
        "links": [
            {"from": "router", "to": "sw", "forward": {"bandwidth_mbps": 4}},
            {"from": "sw", "to": "relay"},
            {"from": "sw", "to": "client", "forward": {"delay_ms": 7}},
        ],
        "subscribers": {},
    }


def switched_topology() -> TopologyConfig:
    data = topology_data()
    # Extra relay stands in for an IP endpoint; no media processes needed.
    data["relays"]["client"] = {"listen_port": 9670, "admin_port": 9671}
    return TopologyConfig.model_validate(data)


def test_switch_build_and_routes_share_router_egress():
    topology = switched_topology()
    record = ContainernetRunRecord(
        run_id="switch-test", run_dir=Path("/tmp/switch-test"),
        loopback_ips=node_loopback_ips(topology),
    )
    build = _FakeBuildNet()
    ContainernetBackend()._build(
        build, topology, {rid: Path(f"/tmp/{rid}.yaml") for rid in topology.relays},
        record, lambda _: None,
    )
    assert build.docker_nodes["sw"]["dimage"] == "moqlab-router"
    assert "sw" not in record.loopback_ips
    assert all(address == "" for _, address in record.node_iface_ips["sw"])
    assert all("10.20.0." in address for node, entries in record.node_iface_ips.items()
               if node != "sw" for _, address in entries)
    net = _FakeNet(all_node_ids(topology))
    ContainernetBackend._configure_network(net, topology, record, lambda _: None)
    bridge = [cmd for node, cmd in net.calls if node == "sw"]
    assert "ip link add br0 type bridge stp_state 0" in bridge
    assert sum("master br0" in cmd for cmd in bridge) == 3
    assert not any("ip route" in cmd or "dualpi2" in cmd for cmd in bridge)
    assert any("dev sw-eth2" in cmd and "netem delay 7ms" in cmd for cmd in bridge)
    routes = [cmd for node, cmd in net.calls if node == "router" and "ip route" in cmd]
    assert len(routes) == 2
    assert all("dev router-eth0" in cmd for cmd in routes)
    assert ("router", "tc qdisc add dev router-eth0 parent 5:1 handle 20: dualpi2") in net.calls


def test_switch_chain_is_one_lan_and_retains_endpoint_identity():
    topology = switched_topology()
    data = topology.model_dump(by_alias=True)
    data["switches"]["sw2"] = {"image": "custom-switch"}
    data["links"][-1]["from"] = "sw2"
    data["links"].append({"from": "sw", "to": "sw2"})
    topology = TopologyConfig.model_validate(data)
    interfaces, neighbors = addressed_links(topology)
    assert neighbors["router"]["client"][1] == "router-eth0"
    assert neighbors["client"]["router"][1] == "client-eth0"
    assert all(not ip for _, ip in interfaces["sw2"])
    ips = node_loopback_ips(topology)
    hops = next_hops(ips, [(a, b) for a, peers in neighbors.items() for b in peers])
    commands = route_commands("client", hops["client"], ips, neighbors["client"])
    assert all(f"src {ips['client']}" in cmd for cmd in commands)
    assert "custom-switch" in topology_image_tags(topology)
    snapshot = topology_snapshot(topology)
    assert snapshot["summary"]["switches"] == 2
    assert {n["id"] for n in snapshot["nodes"] if n["role"] == "switch"} == {"sw", "sw2"}
    assert schema_contract_issues() == []


@pytest.mark.parametrize("case", ["orphan", "duplicate", "loop", "aqm"])
def test_reject_invalid_switch_topologies(case):
    data = switched_topology().model_dump(by_alias=True)
    if case == "orphan":
        data["switches"]["unused"] = {}
    elif case == "aqm":
        data["switches"]["sw"]["aqm"] = "dualpi2"
    else:
        data["switches"].update({"sw2": {}, "sw3": {}})
        data["links"].extend([
            {"from": "sw", "to": "sw2"}, {"from": "sw2", "to": "sw3"},
            {"from": "sw3", "to": "sw" if case == "loop" else "router"},
        ])
    with pytest.raises(ValueError):
        TopologyConfig.model_validate(data)
