from __future__ import annotations

from pathlib import Path

import pytest

from moqlab.config.schema import load_topology
from moqlab.exceptions import OrchestratorError
from moqlab.runtime import (
    containernet_edge_interfaces,
    relay_depths,
    relay_order,
    topology_edges,
    validate_run_id,
)


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


def test_relay_depths_counts_upstream_hops(tmp_path: Path):
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

    assert relay_depths(topology) == {"relay-a": 0, "relay-b": 1, "relay-c": 2}


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


def test_containernet_edge_interfaces_follow_link_order(tmp_path: Path):
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
            ]
        )
    )
    topology = load_topology(config)

    edges = containernet_edge_interfaces(topology)

    assert [(e.a, e.b, e.a_iface, e.b_iface) for e in edges] == [
        ("pub", "relay-a", "pub-eth0", "relay-a-eth0"),
        ("relay-a", "sub", "relay-a-eth1", "sub-eth0"),
    ]


@pytest.mark.parametrize(
    "run_id",
    [
        "run_20260610_120000",
        "myrun",
        "run-1",
        "A",
        "a1-b2_c3",
        "x" * 63,
    ],
)
def test_validate_run_id_accepts_valid(run_id: str) -> None:
    validate_run_id(run_id)


@pytest.mark.parametrize(
    "run_id",
    [
        "../../etc/passwd",
        "../escape",
        "run/sub",
        "run id",
        "",
        "-starts-with-dash",
        "_starts_with_underscore",
        "x" * 64,
    ],
)
def test_validate_run_id_rejects_invalid(run_id: str) -> None:
    with pytest.raises(OrchestratorError, match="invalid run id"):
        validate_run_id(run_id)
