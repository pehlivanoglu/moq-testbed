from __future__ import annotations

from moqlab.config.schema import TopologyConfig
from moqlab.visualizer import (
    InterfaceCounters,
    ThroughputSampler,
    topology_snapshot,
)


def _topology() -> TopologyConfig:
    return TopologyConfig.model_validate(
        {
            "relays": {
                "relay-a": {"listen_port": 9668, "admin_port": 9669},
            },
            "publishers": {
                "pub": {"connects_to": "relay-a", "namespace": "moq-date"},
            },
            "subscribers": {
                "sub": {
                    "connects_to": "relay-a",
                    "namespace": "moq-date",
                    "track": "date",
                }
            },
            "links": [
                {"from": "pub", "to": "relay-a", "bandwidth_mbps": 100},
                {"from": "relay-a", "to": "sub", "delay_ms": 5},
            ],
        }
    )


def test_topology_snapshot_includes_pub_sub_edges_and_shape():
    snapshot = topology_snapshot(_topology())

    assert snapshot["summary"] == {
        "relays": 1,
        "publishers": 1,
        "subscribers": 1,
        "links": 2,
    }
    assert {node["id"] for node in snapshot["nodes"]} == {"relay-a", "pub", "sub"}
    assert snapshot["links"] == [
        {
            "id": "pub--relay-a",
            "source": "pub",
            "target": "relay-a",
            "bandwidth_mbps": 100.0,
            "delay_ms": None,
            "jitter_ms": None,
            "loss_pct": None,
        },
        {
            "id": "relay-a--sub",
            "source": "relay-a",
            "target": "sub",
            "bandwidth_mbps": None,
            "delay_ms": 5.0,
            "jitter_ms": None,
            "loss_pct": None,
        },
    ]


def test_throughput_sampler_reports_unavailable_when_counters_missing():
    sampler = ThroughputSampler(_topology(), counter_reader=lambda _node, _iface: None)

    rates = sampler.sample()

    assert rates["pub--relay-a"]["status"] == "unavailable"
    assert rates["relay-a--sub"]["throughput_bps"] is None


def test_throughput_sampler_uses_containernet_edge_interfaces(monkeypatch):
    now = 10.0
    counters = {
        ("pub", "pub-eth0"): InterfaceCounters(rx_bytes=0, tx_bytes=100),
        ("relay-a", "relay-a-eth0"): InterfaceCounters(rx_bytes=100, tx_bytes=20),
        ("relay-a", "relay-a-eth1"): InterfaceCounters(rx_bytes=0, tx_bytes=0),
        ("sub", "sub-eth0"): InterfaceCounters(rx_bytes=0, tx_bytes=0),
    }

    def fake_monotonic():
        return now

    def fake_reader(node: str, iface: str) -> InterfaceCounters | None:
        return counters.get((node, iface))

    import moqlab.visualizer as visualizer

    monkeypatch.setattr(visualizer.time, "monotonic", fake_monotonic)
    sampler = ThroughputSampler(_topology(), counter_reader=fake_reader)

    assert sampler.sample()["pub--relay-a"]["status"] == "warming"

    now = 12.0
    counters[("pub", "pub-eth0")] = InterfaceCounters(rx_bytes=0, tx_bytes=350)
    counters[("relay-a", "relay-a-eth0")] = InterfaceCounters(rx_bytes=350, tx_bytes=70)

    rates = sampler.sample()

    assert rates["pub--relay-a"]["status"] == "ok"
    assert rates["pub--relay-a"]["a_to_b_bps"] == 1000.0
    assert rates["pub--relay-a"]["b_to_a_bps"] == 200.0
    assert rates["pub--relay-a"]["throughput_bps"] == 1200.0
