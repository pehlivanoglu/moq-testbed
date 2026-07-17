"""Unit tests for Containernet backend behavior.

These tests use fake Mininet nodes so they never require Docker, Containernet,
or root privileges.
"""

from __future__ import annotations

import builtins
import types
from pathlib import Path

import pytest

from moqlab.config.schema import TopologyConfig
from moqlab.exceptions import OrchestratorError
from moqlab.orchestrator.containernet_backend import (
    ContainernetBackend,
    ContainernetRunRecord,
    _write_etc_hosts_via_docker,
)
from moqlab.runtime import node_loopback_ips


class _FakeNode:
    def __init__(self, node_id: str, calls: list[tuple[str, str]]) -> None:
        self.node_id = node_id
        self._calls = calls

    def cmd(self, command: str) -> str:
        self._calls.append((self.node_id, command))
        return ""


class _FakeNet:
    def __init__(self, node_ids: list[str]) -> None:
        self.calls: list[tuple[str, str]] = []
        self._nodes = {nid: _FakeNode(nid, self.calls) for nid in node_ids}

    def get(self, node_id: str) -> _FakeNode:
        return self._nodes[node_id]


class _FakeBuildNet:
    def __init__(self) -> None:
        self.docker_nodes: dict[str, dict] = {}
        self.links: list[tuple[str, str, dict, dict]] = []

    def addDocker(self, node_id: str, **kwargs):
        self.docker_nodes[node_id] = kwargs
        return node_id

    def addLink(self, a, b, params1=None, params2=None, **kwargs):
        self.links.append((a, b, params1 or {}, params2 or {}))


def _topology() -> TopologyConfig:
    return TopologyConfig.model_validate(
        {
            "relays": {
                "relay-a": {"listen_port": 9668, "admin_port": 9669, "upstream": None},
                "relay-b": {"listen_port": 9670, "admin_port": 9671, "upstream": "relay-a"},
                "relay-c": {"listen_port": 9672, "admin_port": 9673, "upstream": "relay-b"},
            },
            "startup": {"relay_warmup_s": 0.5, "publisher_warmup_s": 1.25},
            "publishers": {"pub": {"connects_to": "relay-a", "namespace": "moq-date"}},
            "subscribers": {
                "sub": {"connects_to": "relay-c", "namespace": "moq-date", "track": "date"}
            },
        }
    )


def _routed_topology() -> TopologyConfig:
    """pub — relay-a — rt-1 — sub, with shaping on both rt-1 directions."""
    return TopologyConfig.model_validate(
        {
            "relays": {
                "relay-a": {"listen_port": 9668, "admin_port": 9669, "upstream": None},
            },
            "publishers": {"pub": {"connects_to": "relay-a", "namespace": "moq-date"}},
            "subscribers": {
                "sub": {"connects_to": "relay-a", "namespace": "moq-date", "track": "date"}
            },
            "routers": {"rt-1": {}},
            "links": [
                {"from": "pub", "to": "relay-a", "forward": {"delay_ms": 5}},
                {"from": "relay-a", "to": "rt-1"},
                {
                    "from": "rt-1",
                    "to": "sub",
                    "forward": {"bandwidth_mbps": 20, "aqm": "dualpi2"},
                    "reverse": {"delay_ms": 1},
                },
            ],
        }
    )


def _record_for(topology: TopologyConfig) -> ContainernetRunRecord:
    record = ContainernetRunRecord(
        run_id="run-test",
        run_dir=Path("/tmp/run-test"),
        loopback_ips=node_loopback_ips(topology),
    )
    backend = ContainernetBackend()
    net = _FakeBuildNet()
    backend._build(net, topology, {"relay-a": Path("/tmp/relay-a.yaml")}, record, lambda _: None)
    return record


def test_launches_relays_before_pub_sub(monkeypatch):
    topology = _topology()
    fake_net = _FakeNet(["relay-a", "relay-b", "relay-c", "pub", "sub"])
    record = ContainernetRunRecord(
        run_id="run-test",
        run_dir=Path("/tmp/run-test"),
        relays=["relay-a", "relay-b", "relay-c"],
        publishers=["pub"],
        subscribers=["sub"],
    )
    sleeps: list[float] = []

    monkeypatch.setattr(
        "moqlab.orchestrator.containernet_backend.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    ContainernetBackend._launch_node_binaries(fake_net, topology, record, lambda _: None)

    launched_nodes = [node_id for node_id, _ in fake_net.calls]
    assert launched_nodes == ["relay-a", "relay-b", "relay-c", "pub", "sub"]
    assert sleeps == [0.5, 1.25]
    assert fake_net.calls[0][1].startswith(
        "/usr/local/bin/moqx --config /etc/moqx/relay.yaml"
    )
    assert fake_net.calls[3][1].startswith("/usr/local/bin/moqdateserver")
    assert fake_net.calls[4][1].startswith("/usr/local/bin/moqtextclient")


def test_up_refuses_topology_without_links(tmp_path):
    config = tmp_path / "topology.yaml"
    config.write_text(
        "\n".join(
            [
                "topology_mode: explicit",
                "relays:",
                "  relay-a: { listen_port: 9668, admin_port: 9669 }",
            ]
        )
    )
    backend = ContainernetBackend(runs_dir=tmp_path / "runs")

    with pytest.raises(OrchestratorError, match="requires explicit `links:`"):
        backend.up(config_path=config)


def test_build_adds_routers_with_forwarding_sysctls_and_direct_links():
    topology = _routed_topology()
    net = _FakeBuildNet()
    record = ContainernetRunRecord(
        run_id="run-test",
        run_dir=Path("/tmp/run-test"),
        loopback_ips=node_loopback_ips(topology),
    )

    ContainernetBackend()._build(
        net, topology, {"relay-a": Path("/tmp/relay-a.yaml")}, record, lambda _: None
    )

    assert record.routers == ["rt-1"]
    router_kwargs = net.docker_nodes["rt-1"]
    assert router_kwargs["dimage"] == "moqlab-router"
    assert router_kwargs["sysctls"]["net.ipv4.ip_forward"] == "1"
    assert router_kwargs["sysctls"]["net.ipv4.conf.all.send_redirects"] == "0"
    endpoint_kwargs = net.docker_nodes["sub"]
    assert endpoint_kwargs["sysctls"]["net.ipv4.conf.all.accept_redirects"] == "0"
    assert "net.ipv4.ip_forward" not in endpoint_kwargs["sysctls"]

    # One direct host↔host link per `links:` entry, both sides addressed.
    assert [(a, b) for a, b, _, _ in net.links] == [
        ("pub", "relay-a"),
        ("relay-a", "rt-1"),
        ("rt-1", "sub"),
    ]
    assert net.links[0][2] == {"ip": "10.20.0.1/24"}
    assert net.links[0][3] == {"ip": "10.20.0.2/24"}
    assert record.edge_ips[("rt-1", "sub")] == ("10.20.2.1", "10.20.2.2")
    assert record.node_iface_ips["rt-1"] == [
        ("rt-1-eth0", "10.20.1.2/24"),
        ("rt-1-eth1", "10.20.2.1/24"),
    ]


def test_build_raises_clear_error_when_link_subnet_pool_exhausted():
    subscriber_count = 257
    topology = TopologyConfig.model_validate(
        {
            "relays": {
                "relay-a": {"listen_port": 9668, "admin_port": 9669, "upstream": None}
            },
            "subscribers": {
                f"sub-{idx}": {
                    "connects_to": "relay-a",
                    "namespace": "moq-date",
                    "track": "date",
                }
                for idx in range(subscriber_count)
            },
            "links": [
                {"from": f"sub-{idx}", "to": "relay-a"}
                for idx in range(subscriber_count)
            ],
        }
    )
    net = _FakeBuildNet()
    record = ContainernetRunRecord(run_id="run-test", run_dir=Path("/tmp/run-test"))
    backend = ContainernetBackend()

    with pytest.raises(OrchestratorError, match="link subnet pool"):
        backend._build(
            net,
            topology,
            {"relay-a": Path("/tmp/relay-a.yaml")},
            record,
            lambda _: None,
        )

    assert net.docker_nodes == {}


def test_configure_network_orders_loopbacks_routes_then_shaping():
    topology = _routed_topology()
    record = _record_for(topology)
    fake_net = _FakeNet(["relay-a", "rt-1", "pub", "sub"])

    ContainernetBackend._configure_network(fake_net, topology, record, lambda _: None)

    sub_cmds = [cmd for nid, cmd in fake_net.calls if nid == "sub"]
    lo_idx = sub_cmds.index("ip addr replace 10.99.0.4/32 dev lo")
    offload_idx = sub_cmds.index("ethtool -K sub-eth0 gso off tso off gro off")
    # sub reaches pub (10.99.0.3) via rt-1's IP on the rt-1—sub /24.
    route_idx = sub_cmds.index(
        "ip route replace 10.99.0.3/32 via 10.20.2.1 dev sub-eth0 src 10.99.0.4"
    )
    # reverse shaping of the rt-1—sub link lands on sub's egress.
    shaping_idx = sub_cmds.index(
        "tc qdisc replace dev sub-eth0 root handle 10: netem delay 1ms limit 50000"
    )
    assert lo_idx < offload_idx < route_idx < shaping_idx

    rt_cmds = [cmd for nid, cmd in fake_net.calls if nid == "rt-1"]
    assert "echo 1 > /proc/sys/net/ipv4/ip_forward" in rt_cmds
    assert "tc qdisc replace dev rt-1-eth1 root handle 5: htb default 1" in rt_cmds
    assert "tc qdisc add dev rt-1-eth1 parent 5:1 handle 20: dualpi2" in rt_cmds

    # Router routes endpoints' /32s out of the right interfaces.
    assert (
        "ip route replace 10.99.0.3/32 via 10.20.1.1 dev rt-1-eth0 src 10.99.0.2"
        in rt_cmds
    )


def test_write_etc_hosts_appends_full_mesh_minus_self(monkeypatch):
    execs: list[tuple[str, str]] = []

    class _FakeContainer:
        def __init__(self, name: str) -> None:
            self._name = name

        def exec_run(self, argv):
            assert argv[:2] == ["sh", "-c"]
            execs.append((self._name, argv[2]))
            return types.SimpleNamespace(exit_code=0, output=b"")

    class _FakeContainers:
        def get(self, name: str) -> _FakeContainer:
            return _FakeContainer(name)

    fake_docker = types.SimpleNamespace(
        from_env=lambda: types.SimpleNamespace(
            ping=lambda: None, containers=_FakeContainers()
        ),
        errors=types.SimpleNamespace(NotFound=KeyError),
    )
    monkeypatch.setitem(__import__("sys").modules, "docker", fake_docker)

    record = ContainernetRunRecord(
        run_id="run-test",
        run_dir=Path("/tmp/run-test"),
        loopback_ips={"relay-a": "10.99.0.1", "rt-1": "10.99.0.2", "sub": "10.99.0.3"},
    )

    _write_etc_hosts_via_docker(record, lambda _: None)

    by_container = dict(execs)
    assert set(by_container) == {"mn.relay-a", "mn.rt-1", "mn.sub"}
    assert by_container["mn.sub"] == (
        "printf '10.99.0.1 relay-a\\n10.99.0.2 rt-1\\n' >> /etc/hosts"
    )
    assert "sub" not in by_container["mn.sub"].split(">>")[0].replace("'", " ")


def test_write_etc_hosts_raises_when_docker_sdk_missing(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "docker":
            raise ImportError("missing docker")
        return real_import(name, *args, **kwargs)

    record = ContainernetRunRecord(
        run_id="run-test",
        run_dir=Path("/tmp/run-test"),
        loopback_ips={"relay-a": "10.99.0.1", "sub": "10.99.0.2"},
    )
    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(OrchestratorError, match="Docker SDK is not installed"):
        _write_etc_hosts_via_docker(record, lambda _: None)


def test_write_etc_hosts_raises_when_docker_daemon_unreachable(monkeypatch):
    fake_docker = types.SimpleNamespace(
        from_env=lambda: types.SimpleNamespace(
            ping=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
    )
    record = ContainernetRunRecord(
        run_id="run-test",
        run_dir=Path("/tmp/run-test"),
        loopback_ips={"relay-a": "10.99.0.1", "sub": "10.99.0.2"},
    )
    monkeypatch.setitem(__import__("sys").modules, "docker", fake_docker)

    with pytest.raises(OrchestratorError, match="Docker daemon is unreachable"):
        _write_etc_hosts_via_docker(record, lambda _: None)
