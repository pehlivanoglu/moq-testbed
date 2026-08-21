"""Unit tests for Containernet backend behavior.

These tests use fake Mininet nodes so they never require Docker, Containernet,
or root privileges.
"""

from __future__ import annotations

import builtins
import types
from pathlib import Path

import pytest

from moqlab.config.schema import AqmKind, DirectionSpec, TopologyConfig
from moqlab.exceptions import OrchestratorError
from moqlab.orchestrator.containernet_backend import (
    ContainernetBackend,
    ContainernetRunRecord,
    _await_containernet_media_ready,
    _await_containernet_native_media_ready,
    _write_etc_hosts_via_docker,
    apply_live_link_shaping,
    apply_live_router_aqm,
)
from moqlab.runtime import node_loopback_ips


class _FakeNode:
    def __init__(self, node_id: str, calls: list[tuple[str, str]]) -> None:
        self.node_id = node_id
        self._calls = calls

    def cmd(self, command: str) -> str:
        self._calls.append((self.node_id, command))
        return ""


class _ReadyNode:
    def cmd(self, command: str) -> str:
        if "__MOQLAB_READY__" in command:
            return "background PTY output\r\n__MOQLAB_READY__\r\n"
        return ""


class _NativeReadyNode:
    def cmd(self, command: str) -> str:
        if "group start" in command:
            return '__MOQLAB_READY__\n'
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
        self.link_options: list[dict] = []

    def addDocker(self, node_id: str, **kwargs):
        self.docker_nodes[node_id] = kwargs
        return node_id

    def addLink(self, a, b, params1=None, params2=None, **kwargs):
        self.links.append((a, b, params1 or {}, params2 or {}))
        self.link_options.append(kwargs)


def _topology() -> TopologyConfig:
    return TopologyConfig.model_validate(
        {
            "relays": {
                "relay-a": {"listen_port": 9668, "admin_port": 9669, "upstream": None},
                "relay-b": {"listen_port": 9670, "admin_port": 9671, "upstream": "relay-a"},
                "relay-c": {"listen_port": 9672, "admin_port": 9673, "upstream": "relay-b"},
            },
            "startup": {"relay_warmup_s": 0.5, "publisher_warmup_s": 1.25},
            "publishers": {"pub": {"connects_to": "relay-a"}},
            "subscribers": {
                "sub": {"connects_to": "relay-c", "namespace": "msf/clear", "track": "video/s2"}
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
            "publishers": {"pub": {"connects_to": "relay-a"}},
            "subscribers": {
                "sub": {"connects_to": "relay-a", "namespace": "msf/clear", "track": "video/s2"}
            },
            "routers": {"rt-1": {"aqm": "dualpi2"}},
            "links": [
                {"from": "pub", "to": "relay-a", "forward": {"delay_ms": 5}},
                {"from": "relay-a", "to": "rt-1"},
                {
                    "from": "rt-1",
                    "to": "sub",
                    "forward": {"bandwidth_mbps": 20},
                    "reverse": {"delay_ms": 1},
                },
            ],
        }
    )


def _traffic_topology() -> TopologyConfig:
    return TopologyConfig.model_validate(
        {
            "startup": {"relay_warmup_s": 0},
            "relays": {"relay-a": {"listen_port": 9668, "admin_port": 9669}},
            "routers": {"west": {}, "east": {}},
            "traffic": {
                "sender": {"id": "tx"},
                "receiver": {"id": "rx"},
                "routes": {
                    "west": {"path": ["tx", "west", "rx"]},
                    "east": {"path": ["tx", "east", "rx"]},
                },
                "flows": [
                    {
                        "id": "load",
                        "kind": "bulk",
                        "route": "west",
                        "duration_s": 1,
                    }
                ],
            },
            "links": [
                {"from": "tx", "to": "west"},
                {"from": "west", "to": "rx"},
                {"from": "tx", "to": "east"},
                {"from": "east", "to": "rx"},
            ],
        }
    )


def test_live_link_shaping_replaces_and_clears_one_direction():
    topology = _routed_topology()
    calls = []
    run = lambda node, command: calls.append((node, command)) or (0, "")

    apply_live_link_shaping(
        topology,
        2,
        "forward",
        DirectionSpec(bandwidth_mbps=10, loss_pct=2),
        topology.links[2].forward,
        run,
    )
    apply_live_link_shaping(
        topology,
        2,
        "reverse",
        DirectionSpec(),
        topology.links[2].reverse,
        run,
    )

    assert calls[0] == (
        "rt-1",
        "tc qdisc replace dev rt-1-eth1 root handle 5: htb default 1",
    )
    assert ("sub", "tc qdisc del dev sub-eth0 root") in calls


def test_live_router_aqm_updates_every_router_egress():
    topology = _routed_topology()
    calls = []
    run = lambda node, command: calls.append((node, command)) or (0, "")

    apply_live_router_aqm(
        topology, "rt-1", None, AqmKind.dualpi2, run
    )

    assert {command.split()[4] for node, command in calls if node == "rt-1"} == {
        "rt-1-eth0",
        "rt-1-eth1",
    }
    assert topology.routers["rt-1"].aqm is None


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


def test_media_readiness_accepts_marker_amid_pty_output():
    _await_containernet_media_ready(_ReadyNode(), "sub", 0, lambda _: None)


def test_native_media_readiness_accepts_first_group_log():
    _await_containernet_native_media_ready(
        _NativeReadyNode(), "sub", "video/s2", 0.01, lambda _: None
    )


def test_launches_media_origin_before_relays_and_subscriber(monkeypatch):
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
    monkeypatch.setattr(
        "moqlab.orchestrator.containernet_backend._await_containernet_media_ready",
        lambda *_args: None,
    )

    ContainernetBackend._launch_node_binaries(fake_net, topology, record, lambda _: None)

    launched_nodes = [node_id for node_id, _ in fake_net.calls]
    assert launched_nodes == ["pub", "relay-a", "relay-b", "relay-c", "sub"]
    assert sleeps == [1.25, 0.5]
    assert fake_net.calls[0][1].startswith("/usr/local/bin/mlmpub")
    assert fake_net.calls[1][1].startswith(
        "/usr/local/bin/moqx --config /etc/moqx/relay.yaml"
    )
    assert fake_net.calls[4][1].startswith("/usr/local/bin/moqlab-media-sub")


def test_launches_media_origin_before_relays_and_browser(monkeypatch):
    topology = TopologyConfig.model_validate(
        {
            "defaults": {"relay": {"tls": {"insecure": False, "generated": True}}},
            "startup": {"relay_warmup_s": 0, "publisher_warmup_s": 0},
            "relays": {"root": {"listen_port": 9668, "admin_port": 9669}},
            "publishers": {
                "pub": {
                    "kind": "media", "connects_to": "root", "asset": "testsvc",
                    "listen_port": 4443, "fingerprint_port": 8081,
                }
            },
            "subscribers": {
                "sub": {
                    "kind": "media", "connects_to": "root", "namespace": "msf/clear",
                    "track": "video/s2",
                }
            },
        }
    )
    net = _FakeNet(["pub", "root", "sub"])
    record = ContainernetRunRecord(
        run_id="run-test", run_dir=Path("/tmp/run-test"),
        relays=["root"], publishers=["pub"], subscribers=["sub"],
    )
    ready = []
    monkeypatch.setattr(
        "moqlab.orchestrator.containernet_backend._await_containernet_media_ready",
        lambda _node, node_id, _timeout, _info: ready.append(node_id),
    )

    ContainernetBackend._launch_node_binaries(net, topology, record, lambda _: None)

    assert [node for node, _ in net.calls] == ["pub", "root", "sub"]
    assert net.calls[0][1].startswith("/usr/local/bin/mlmpub")
    assert net.calls[2][1].startswith("/usr/local/bin/moqlab-media-sub")
    assert ready == ["sub"]


def test_launches_native_media_subscriber_without_browser_runner(monkeypatch):
    topology = TopologyConfig.model_validate(
        {
            "defaults": {
                "relay": {"tls": {"insecure": False, "generated": True}},
                "subscriber": {"media_client": "native"},
            },
            "startup": {"relay_warmup_s": 0, "publisher_warmup_s": 0},
            "relays": {"root": {"listen_port": 9668, "admin_port": 9669}},
            "publishers": {
                "pub": {
                    "kind": "media", "connects_to": "root", "asset": "testsvc",
                    "listen_port": 4443, "fingerprint_port": 8081,
                }
            },
            "subscribers": {
                "sub": {
                    "kind": "media", "connects_to": "root",
                    "namespace": "msf/clear", "track": "video/s2",
                }
            },
        }
    )
    net = _FakeNet(["pub", "root", "sub"])
    record = ContainernetRunRecord(
        run_id="run-test", run_dir=Path("/tmp/run-test"),
        relays=["root"], publishers=["pub"], subscribers=["sub"],
    )
    ready = []
    monkeypatch.setattr(
        "moqlab.orchestrator.containernet_backend._await_containernet_native_media_ready",
        lambda _node, node_id, _track, _timeout, _info: ready.append(node_id),
    )

    ContainernetBackend._launch_node_binaries(net, topology, record, lambda _: None)

    assert net.calls[2][1].startswith("/usr/local/bin/mlmsub")
    assert "-subscribe-dependencies" in net.calls[2][1]
    assert ready == ["sub"]


def test_x11_media_subscriber_mounts_host_display(monkeypatch):
    topology = TopologyConfig.model_validate(
        {
            "defaults": {"relay": {"tls": {"insecure": False, "generated": True}}},
            "relays": {"root": {"listen_port": 9668, "admin_port": 9669}},
            "publishers": {
                "pub": {
                    "kind": "media", "connects_to": "root", "asset": "testsvc",
                    "listen_port": 4443, "fingerprint_port": 8081,
                }
            },
            "subscribers": {
                "sub": {
                    "kind": "media", "connects_to": "root", "namespace": "msf/clear",
                    "track": "video/s2", "media_client": "chrome",
                }
            },
            "links": [
                {"from": "pub", "to": "root"},
                {"from": "root", "to": "sub"},
            ],
        }
    )
    monkeypatch.setenv("DISPLAY", ":7")
    net = _FakeBuildNet()
    record = ContainernetRunRecord(run_id="run-test", run_dir=Path("/tmp/run-test"))

    ContainernetBackend()._build(
        net, topology, {"root": Path("/tmp/root.yaml")}, record, lambda _: None
    )

    assert net.docker_nodes["sub"]["environment"] == {"DISPLAY": ":7"}
    assert net.docker_nodes["sub"]["volumes"] == [
        "/tmp/.X11-unix:/tmp/.X11-unix:rw"
    ]


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
    assert net.link_options == [
        {"intfName1": "pub-eth0", "intfName2": "relay-a-eth0"},
        {"intfName1": "relay-a-eth1", "intfName2": "rt-1-eth0"},
        {"intfName1": "rt-1-eth1", "intfName2": "sub-eth0"},
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
            "publishers": {"pub": {"connects_to": "relay-a"}},
            "subscribers": {
                f"sub-{idx}": {
                    "connects_to": "relay-a",
                    "namespace": "moq-date",
                    "track": "date",
                }
                for idx in range(subscriber_count)
            },
            "links": [
                {"from": "pub", "to": "relay-a"},
                *(
                    {"from": f"sub-{idx}", "to": "relay-a"}
                    for idx in range(subscriber_count)
                ),
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


def test_traffic_alias_routes_follow_each_explicit_path():
    topology = _traffic_topology()
    record = ContainernetRunRecord(
        run_id="run-test",
        run_dir=Path("/tmp/run-test"),
        loopback_ips=node_loopback_ips(topology),
    )
    build_net = _FakeBuildNet()
    ContainernetBackend()._build(
        build_net,
        topology,
        {"relay-a": Path("/tmp/relay-a.yaml")},
        record,
        lambda _: None,
    )
    fake_net = _FakeNet(["relay-a", "west", "east", "tx", "rx"])

    ContainernetBackend._configure_network(fake_net, topology, record, lambda _: None)

    tx_commands = [command for node, command in fake_net.calls if node == "tx"]
    west_commands = [command for node, command in fake_net.calls if node == "west"]
    east_commands = [command for node, command in fake_net.calls if node == "east"]
    assert "ip addr replace 10.100.0.1/32 dev lo" in tx_commands
    assert (
        "ip route replace 10.101.0.1/32 via 10.20.0.2 dev tx-eth0 src 10.100.0.1"
        in tx_commands
    )
    assert "ip route replace 10.101.0.1/32 via 10.20.1.2 dev west-eth1" in west_commands
    assert "ip route replace 10.101.0.2/32 via 10.20.3.2 dev east-eth1" in east_commands


def test_launches_one_traffic_receiver_then_one_sender(monkeypatch):
    topology = _traffic_topology()
    net = _FakeNet(["relay-a", "tx", "rx"])
    record = ContainernetRunRecord(
        run_id="run-test",
        run_dir=Path("/tmp/run-test"),
        relays=["relay-a"],
        traffic_endpoints=["tx", "rx"],
    )
    monkeypatch.setattr(
        "moqlab.orchestrator.containernet_backend._await_traffic_receiver",
        lambda _node, _node_id, _timeout: None,
    )

    ContainernetBackend._launch_node_binaries(net, topology, record, lambda _: None)

    assert [node for node, _ in net.calls] == ["relay-a", "rx", "tx"]
    assert "moqlab-traffic receiver" in net.calls[1][1]
    assert "moqlab-traffic sender" in net.calls[2][1]


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
