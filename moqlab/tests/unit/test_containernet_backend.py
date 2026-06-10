"""Unit tests for Containernet backend launch behavior.

These tests use fake Mininet nodes so they never require Docker, Containernet,
or root privileges.
"""

from __future__ import annotations

from pathlib import Path

from moqlab.config.schema import TopologyConfig
from moqlab.orchestrator.containernet_backend import (
    ContainernetBackend,
    ContainernetRunRecord,
    _htb_r2q_for_bw,
    _is_qdisc_root_delete,
    _qdisc_show_has_deletable_root,
)


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


def test_htb_r2q_targets_packet_sized_quantum():
    assert _htb_r2q_for_bw(100) == 8333
    assert _htb_r2q_for_bw(50) == 4167
    assert _htb_r2q_for_bw(20) == 1667
    assert _htb_r2q_for_bw(0) == 10


def test_qdisc_delete_precheck_skips_only_zero_handle_roots():
    assert _is_qdisc_root_delete("%s qdisc del dev %s root")
    assert not _is_qdisc_root_delete("%s qdisc add dev %s root handle 5:0 htb")

    assert not _qdisc_show_has_deletable_root(
        "qdisc fq_codel 0: root refcnt 2 limit 10240p\r\n"
    )
    assert not _qdisc_show_has_deletable_root("qdisc noqueue 0: root refcnt 2\r\n")
    assert not _qdisc_show_has_deletable_root("")
    assert _qdisc_show_has_deletable_root(
        "qdisc htb 5: root refcnt 2 r2q 8333 default 0x1\r\n"
    )
    assert _qdisc_show_has_deletable_root(
        "RTNETLINK answers: Operation not permitted\r\n"
    )
