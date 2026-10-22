from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

import pytest

from moqlab import trafficgen
from moqlab.config.schema import TopologyConfig, load_topology
from moqlab.designer import dump_draft_yaml, parse_yaml_draft
from moqlab.exceptions import OrchestratorError
from moqlab.orchestrator import containernet_backend as backend
from moqlab.runtime import all_node_ids, node_loopback_ips, traffic_plan
from moqlab.visualizer import topology_snapshot
from test_containernet_backend import _FakeBuildNet, _FakeNet
from test_traffic import _traffic_topology


EXAMPLE = Path(__file__).resolve().parents[2] / "configs/examples/external_traffic_multi.yaml"


def test_legacy_traffic_import_normalizes_without_mutating_input():
    topology = _traffic_topology()
    data = topology.model_dump(by_alias=True)
    for role in ("sender", "receiver"):
        data["traffic"][role] = data["traffic"].pop(f"{role}s")[0]
    imported = parse_yaml_draft(dump_draft_yaml(data))

    assert "sender" in data["traffic"]
    assert "sender" not in imported["traffic"]
    assert imported["traffic"]["senders"] == [{"id": "tx", "image": None}]
    assert TopologyConfig.model_validate(imported) == topology


@pytest.mark.parametrize("change, message", [
    (lambda t: t["senders"].append({"id": "tx"}), "reused"),
    (lambda t: t["receivers"].append({"id": "tx"}), "reused"),
    (lambda t: t["senders"].append({"id": "relay"}), "reused"),
    (lambda t: t.update(senders=[]), "at least 1"),
    (lambda t: t.update(sender={"id": "extra"}), "not both"),
    (lambda t: t["routes"]["first"].update(path=["rx", "router", "tx"]), "declared sender"),
    (lambda t: t["routes"]["first"].update(path=["missing", "router", "rx"]), "declared sender"),
    (lambda t: t["routes"]["first"].update(path=["tx", "router", "missing"]), "declared receiver"),
])
def test_multi_endpoint_validation(change, message):
    data = load_topology(EXAMPLE).model_dump(by_alias=True)
    change(data["traffic"])
    with pytest.raises(ValueError, match=message):
        TopologyConfig.model_validate(data)


def test_multi_endpoint_build_routes_and_visualizer(tmp_path):
    topology = load_topology(EXAMPLE)
    record = backend.ContainernetRunRecord(
        run_id="multi", run_dir=tmp_path, loopback_ips=node_loopback_ips(topology)
    )
    build = _FakeBuildNet()
    backend.ContainernetBackend()._build(
        build, topology, {"relay": tmp_path / "relay.yaml"}, record, lambda _: None
    )
    assert record.traffic_endpoints == ["tx", "tx2", "rx", "rx2"]
    assert all(build.docker_nodes[nid]["dimage"] == "moqlab-traffic" for nid in record.traffic_endpoints)
    net = _FakeNet(all_node_ids(topology))
    backend.ContainernetBackend._configure_network(net, topology, record, lambda _: None)
    plan = traffic_plan(topology)
    for route in plan["routes"].values():
        for role, node in (("sender", route["path"][0]), ("receiver", route["path"][-1])):
            alias = f"ip addr replace {route[f'{role}_ip']}/32 dev lo"
            assert [nid for nid, cmd in net.calls if cmd == alias] == [node]
        sender, receiver = route["path"][0], route["path"][-1]
        assert any(nid == sender and cmd.startswith(f"ip route replace {route['receiver_ip']}/32 via ")
                   and cmd.endswith(f" src {route['sender_ip']}") for nid, cmd in net.calls)
        assert any(nid == receiver and cmd.startswith(f"ip route replace {route['sender_ip']}/32 via ")
                   and cmd.endswith(f" src {route['receiver_ip']}") for nid, cmd in net.calls)
        for address in (route["sender_ip"], route["receiver_ip"]):
            assert any(nid == "router" and cmd.startswith(f"ip route replace {address}/32 via ")
                       for nid, cmd in net.calls)
    assert not any(nid == "switch" and "ip route" in cmd for nid, cmd in net.calls)
    snapshot = topology_snapshot(topology)
    assert snapshot["summary"]["traffic_endpoints"] == 4
    roles = {node["id"]: node["role"] for node in snapshot["nodes"]}
    assert {nid for nid, role in roles.items() if role == "traffic-sender"} == {"tx", "tx2"}
    assert {nid for nid, role in roles.items() if role == "traffic-receiver"} == {"rx", "rx2"}


def test_all_receivers_ready_before_senders_and_all_senders_stop(monkeypatch, tmp_path):
    topology = load_topology(EXAMPLE)
    topology.startup.relay_warmup_s = 0
    net = _FakeNet(all_node_ids(topology))
    record = backend.ContainernetRunRecord(run_id="multi", run_dir=tmp_path, relays=["relay"])
    ready = []

    def await_receiver(node, node_id, timeout):
        assert not any("moqlab-traffic sender" in cmd for _, cmd in net.calls)
        assert sum("moqlab-traffic receiver" in cmd for _, cmd in net.calls) == 2
        ready.append(node_id)

    monkeypatch.setattr(backend, "_await_traffic_receiver", await_receiver)
    backend.ContainernetBackend._launch_node_binaries(net, topology, record, lambda _: None)
    assert ready == ["rx", "rx2"]
    senders = [(nid, shlex.split(cmd)) for nid, cmd in net.calls if "moqlab-traffic sender" in cmd]
    assert [nid for nid, _ in senders] == ["tx", "tx2"]
    assert len({args[args.index("--epoch") + 1] for _, args in senders}) == 1
    for nid, args in senders:
        assert args[args.index("--node") + 1] == nid
    stopped = []

    def stop(node, command):
        stopped.append(node.node_id)
        return (1, "failed") if node.node_id == "tx" else (0, "")

    monkeypatch.setattr(backend, "_node_command_status", stop)
    with pytest.raises(OrchestratorError, match="tx: failed"):
        backend._stop_traffic_sender(net, topology)
    assert stopped == ["tx", "tx2"]


def test_sender_flow_ownership_and_global_udp_indices(monkeypatch):
    plan = traffic_plan(load_topology(EXAMPLE))
    calls = []

    async def flow(_plan, flow, index, epoch):
        calls.append((flow["id"], index, epoch))

    monkeypatch.setattr(trafficgen, "_run_flow", flow)
    asyncio.run(trafficgen._run_sender(plan, "tx2", 123.0))
    assert calls == [("video", 1, 123.0), ("voice", 2, 123.0)]
    calls.clear()
    asyncio.run(trafficgen._run_sender(plan, "tx", 123.0))
    assert calls == [("bulk", 0, 123.0), ("segments", 3, 123.0)]
    events = []
    monkeypatch.setattr(trafficgen, "_log", lambda event, **fields: events.append((event, fields)))
    receiver = trafficgen._UdpReceiver([flow["id"] for flow in plan["flows"]])
    receiver.datagram_received(trafficgen._UDP_HEADER.pack(2, 0), None)
    receiver._report()
    assert events[0][1]["flow_id"] == "voice"


def test_validate_reports_all_endpoint_counts_and_images():
    from click.testing import CliRunner
    from moqlab.cli import cli

    result = CliRunner().invoke(cli, ["validate", "-c", str(EXAMPLE)])
    assert result.exit_code == 0, result.output
    assert "traffic_endpoints=4" in result.output
    for node in ("tx", "tx2", "rx", "rx2"):
        assert f"{node:14}" in result.output
