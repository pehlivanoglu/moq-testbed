from __future__ import annotations

import pytest

from moqlab.config.schema import TopologyConfig
from moqlab.runtime import all_node_ids, topology_image_tags, traffic_plan


def _traffic_topology() -> TopologyConfig:
    return TopologyConfig.model_validate(
        {
            "relays": {"relay": {"listen_port": 9668, "admin_port": 9669}},
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
                        "id": "bulk",
                        "kind": "bulk",
                        "route": "east",
                        "duration_s": 10,
                        "connections": 2,
                    },
                    {
                        "id": "video",
                        "kind": "segmented",
                        "route": "west",
                        "duration_s": 12,
                        "segment_duration_ms": 2000,
                        "representation_sequence_mbps": [2, 5],
                    },
                    {
                        "id": "voice",
                        "kind": "cbr",
                        "route": "west",
                        "duration_s": 5,
                        "rate_mbps": 1,
                    },
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


def test_traffic_plan_assigns_stable_route_aliases() -> None:
    topology = _traffic_topology()

    plan = traffic_plan(topology)

    assert plan["routes"] == {
        "west": {
            "sender_ip": "10.100.0.1",
            "receiver_ip": "10.101.0.1",
            "path": ["tx", "west", "rx"],
        },
        "east": {
            "sender_ip": "10.100.0.2",
            "receiver_ip": "10.101.0.2",
            "path": ["tx", "east", "rx"],
        },
    }
    assert [flow["kind"] for flow in plan["flows"]] == [
        "bulk",
        "segmented",
        "cbr",
    ]
    assert all_node_ids(topology)[-2:] == ["tx", "rx"]
    assert "moqlab-traffic" in topology_image_tags(topology)


def test_traffic_route_rejects_undeclared_hop() -> None:
    data = _traffic_topology().model_dump(by_alias=True)
    data["traffic"]["routes"]["west"]["path"] = ["tx", "west", "east", "rx"]

    with pytest.raises(ValueError, match="undeclared link"):
        TopologyConfig.model_validate(data)


def test_traffic_route_rejects_application_node_as_intermediate() -> None:
    data = _traffic_topology().model_dump(by_alias=True)
    data["traffic"]["routes"]["west"]["path"] = ["tx", "relay", "rx"]
    data["links"].extend(
        [{"from": "tx", "to": "relay"}, {"from": "relay", "to": "rx"}]
    )

    with pytest.raises(ValueError, match="must be a router"):
        TopologyConfig.model_validate(data)
