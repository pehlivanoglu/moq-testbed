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
