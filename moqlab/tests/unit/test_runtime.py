from __future__ import annotations

from pathlib import Path

from moqlab.config.schema import load_topology
from moqlab.runtime import relay_order, topology_edges


def test_relay_order_sorts_upstreams_before_downstreams(tmp_path: Path):
    config = tmp_path / "topology.yaml"
    config.write_text(
        "\n".join(
            [
                "topology_mode: explicit",
                "relays:",
                "  relay-c: { listen_port: 9672, admin_port: 9673, upstream: relay-b }",
                "  relay-a: { listen_port: 9668, admin_port: 9669 }",
                "  relay-b: { listen_port: 9670, admin_port: 9671, upstream: relay-a }",
            ]
        )
    )

    topology = load_topology(config)

    assert relay_order(topology) == ["relay-a", "relay-b", "relay-c"]


def test_topology_edges_dedupes_undirected_edges(tmp_path: Path):
    config = tmp_path / "topology.yaml"
    config.write_text(
        "\n".join(
            [
                "topology_mode: explicit",
                "relays:",
                "  relay-a: { listen_port: 9668, admin_port: 9669 }",
                "publishers:",
                "  pub: { connects_to: relay-a, namespace: moq-date }",
                "subscribers:",
                "  sub: { connects_to: relay-a, namespace: moq-date, track: date }",
                "links:",
                "  - { from: relay-a, to: pub, delay_ms: 5 }",
            ]
        )
    )

    topology = load_topology(config)

    assert topology_edges(topology) == [("pub", "relay-a"), ("relay-a", "sub")]
