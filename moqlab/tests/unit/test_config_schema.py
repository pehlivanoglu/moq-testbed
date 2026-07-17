"""Unit tests for moqlab.config.schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from moqlab.config.schema import TopologyConfig, load_topology
from moqlab.exceptions import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = REPO_ROOT / "moqlab" / "configs" / "examples" / "linear_3r_1s.yaml"


def _minimal_relays() -> dict:
    return {
        "relay-a": {"listen_port": 9668, "admin_port": 9669, "upstream": None},
        "relay-b": {"listen_port": 9670, "admin_port": 9671, "upstream": "relay-a"},
    }


# ── example round-trip ─────────────────────────────────────────────────────


def test_loads_linear_3r_1s_example():
    t = load_topology(EXAMPLE)
    assert set(t.relays.keys()) == {"relay-a", "relay-b", "relay-c"}
    assert set(t.publishers.keys()) == {"pub"}
    assert set(t.subscribers.keys()) == {"sub"}
    assert set(t.routers.keys()) == {"rt-ab", "rt-bc"}
    assert len(t.links) == 6
    assert t.startup.publisher_warmup_s == 1.0
    assert t.relays["relay-c"].upstream == "relay-b"
    assert t.publishers["pub"].connects_to == "relay-a"
    assert t.subscribers["sub"].connects_to == "relay-c"
    bottleneck = t.link_for("rt-ab", "relay-b")
    assert bottleneck is not None
    assert bottleneck.forward.aqm is not None
    assert bottleneck.forward.bandwidth_mbps == 50
    assert t.router_image("rt-ab") == "moqlab-router"


# ── relay rules ────────────────────────────────────────────────────────────


def test_minimum_topology_validates():
    t = TopologyConfig.model_validate({"relays": _minimal_relays()})
    assert t.topology_mode == "explicit"
    assert t.defaults.relay.image == "moqlab-relay"
    assert t.defaults.publisher.image == "moqlab-pub"
    assert t.defaults.subscriber.image == "moqlab-sub"
    assert t.startup.relay_warmup_s == 2.0
    assert t.startup.publisher_warmup_s == 1.0


def test_startup_warmups_are_configurable():
    t = TopologyConfig.model_validate(
        {
            "startup": {"relay_warmup_s": 0.25, "publisher_warmup_s": 1.5},
            "relays": _minimal_relays(),
        }
    )
    assert t.startup.relay_warmup_s == 0.25
    assert t.startup.publisher_warmup_s == 1.5


def test_rejects_negative_startup_warmup():
    with pytest.raises(Exception):
        TopologyConfig.model_validate(
            {
                "startup": {"publisher_warmup_s": -1},
                "relays": _minimal_relays(),
            }
        )


def test_rejects_empty_relays():
    with pytest.raises(Exception, match="at least one relay"):
        TopologyConfig.model_validate({"relays": {}})


def test_rejects_unknown_upstream():
    relays = _minimal_relays()
    relays["relay-b"]["upstream"] = "ghost"
    with pytest.raises(Exception, match="not a known relay"):
        TopologyConfig.model_validate({"relays": relays})


def test_rejects_cycle():
    relays = {
        "relay-a": {"listen_port": 9668, "admin_port": 9669, "upstream": "relay-b"},
        "relay-b": {"listen_port": 9670, "admin_port": 9671, "upstream": "relay-a"},
    }
    with pytest.raises(Exception, match="cycle"):
        TopologyConfig.model_validate({"relays": relays})


def test_rejects_duplicate_ports_across_relays():
    relays = _minimal_relays()
    relays["relay-b"]["admin_port"] = 9668
    with pytest.raises(Exception, match="collides"):
        TopologyConfig.model_validate({"relays": relays})


def test_rejects_same_listen_and_admin_on_one_relay():
    relays = {"r1": {"listen_port": 9668, "admin_port": 9668, "upstream": None}}
    with pytest.raises(Exception, match="differ"):
        TopologyConfig.model_validate({"relays": relays})


def test_rejects_invalid_relay_id():
    relays = {"bad name!": {"listen_port": 9668, "admin_port": 9669, "upstream": None}}
    with pytest.raises(Exception, match="invalid relay id"):
        TopologyConfig.model_validate({"relays": relays})


# ── publishers ─────────────────────────────────────────────────────────────


def test_publisher_must_target_known_relay():
    raw = {
        "relays": _minimal_relays(),
        "publishers": {"pub": {"connects_to": "ghost", "namespace": "x"}},
    }
    with pytest.raises(Exception, match="not a known relay"):
        TopologyConfig.model_validate(raw)


def test_publisher_id_collides_with_relay_id():
    raw = {
        "relays": _minimal_relays(),
        "publishers": {"relay-a": {"connects_to": "relay-a", "namespace": "x"}},
    }
    with pytest.raises(Exception, match="reused"):
        TopologyConfig.model_validate(raw)


# ── subscribers ────────────────────────────────────────────────────────────


def test_subscriber_must_target_known_relay():
    raw = {
        "relays": _minimal_relays(),
        "subscribers": {"sub": {"connects_to": "ghost", "namespace": "x", "track": "t"}},
    }
    with pytest.raises(Exception, match="not a known relay"):
        TopologyConfig.model_validate(raw)


# ── links ──────────────────────────────────────────────────────────────────


def test_links_validate_endpoints_exist():
    raw = {
        "relays": _minimal_relays(),
        "links": [{"from": "relay-a", "to": "ghost"}],
    }
    with pytest.raises(Exception, match="unknown node"):
        TopologyConfig.model_validate(raw)


def test_rejects_duplicate_undirected_link():
    raw = {
        "relays": _minimal_relays(),
        "links": [
            {"from": "relay-a", "to": "relay-b"},
            {"from": "relay-b", "to": "relay-a"},
        ],
    }
    with pytest.raises(Exception, match="duplicate link"):
        TopologyConfig.model_validate(raw)


def test_rejects_self_link():
    raw = {
        "relays": _minimal_relays(),
        "links": [{"from": "relay-a", "to": "relay-a"}],
    }
    with pytest.raises(Exception, match="endpoints must differ"):
        TopologyConfig.model_validate(raw)


def test_rejects_old_flat_link_fields():
    raw = {
        "relays": _minimal_relays(),
        "links": [{"from": "relay-a", "to": "relay-b", "delay_ms": 5.0}],
    }
    with pytest.raises(Exception):
        TopologyConfig.model_validate(raw)


def test_rejects_app_edge_without_link_path():
    raw = {
        "relays": _minimal_relays(),
        "subscribers": {"sub": {"connects_to": "relay-b", "namespace": "x", "track": "t"}},
        "links": [{"from": "relay-a", "to": "relay-b"}],
    }
    with pytest.raises(Exception, match="no path of links connects"):
        TopologyConfig.model_validate(raw)


def test_accepts_app_edge_path_through_router():
    t = TopologyConfig.model_validate(
        {
            "relays": _minimal_relays(),
            "routers": {"rt-1": {}},
            "links": [
                {"from": "relay-a", "to": "rt-1"},
                {"from": "rt-1", "to": "relay-b"},
            ],
        }
    )
    assert set(t.routers) == {"rt-1"}


def test_link_for_lookup_is_endpoint_order_agnostic():
    t = TopologyConfig.model_validate(
        {
            "relays": _minimal_relays(),
            "links": [
                {
                    "from": "relay-a",
                    "to": "relay-b",
                    "forward": {"delay_ms": 5.0},
                }
            ],
        }
    )
    link = t.link_for("relay-b", "relay-a")
    assert link is not None
    assert link.forward.delay_ms == 5.0
    assert link.reverse.is_noop()


def test_link_for_allows_pub_sub_links():
    t = TopologyConfig.model_validate(
        {
            "relays": _minimal_relays(),
            "publishers": {"pub": {"connects_to": "relay-a", "namespace": "x"}},
            "subscribers": {"sub": {"connects_to": "relay-b", "namespace": "x", "track": "t"}},
            "links": [
                {"from": "pub", "to": "relay-a", "forward": {"delay_ms": 5.0}},
                {"from": "relay-a", "to": "relay-b"},
                {"from": "relay-b", "to": "sub", "forward": {"delay_ms": 10.0}},
            ],
        }
    )
    assert t.link_for("relay-a", "pub") is not None
    assert t.link_for("sub", "relay-b") is not None


# ── defaults.link inheritance ──────────────────────────────────────────────


def _with_link_defaults(link: dict, **extra) -> TopologyConfig:
    return TopologyConfig.model_validate(
        {
            "relays": _minimal_relays(),
            "defaults": {
                "link": {
                    "forward": {"delay_ms": 1.0},
                    "reverse": {"delay_ms": 1.0},
                }
            },
            "links": [link],
            **extra,
        }
    )


def test_link_inherits_defaults_when_direction_omitted():
    t = _with_link_defaults({"from": "relay-a", "to": "relay-b"})
    link = t.links[0]
    assert link.forward.delay_ms == 1.0
    assert link.reverse.delay_ms == 1.0


def test_link_defaults_merge_per_field_not_per_block():
    # Setting bandwidth must not wipe the inherited delay: the whole point of
    # `defaults.link` is that a link states only what differs.
    t = _with_link_defaults(
        {"from": "relay-a", "to": "relay-b", "forward": {"bandwidth_mbps": 20.0}}
    )
    assert t.links[0].forward.bandwidth_mbps == 20.0
    assert t.links[0].forward.delay_ms == 1.0


def test_link_overrides_inherited_field():
    t = _with_link_defaults(
        {"from": "relay-a", "to": "relay-b", "forward": {"delay_ms": 25.0}}
    )
    assert t.links[0].forward.delay_ms == 25.0


def test_explicit_zero_overrides_inherited_delay():
    # 0 is a value, not "unset": truthiness-based merging would drop it back
    # to the inherited 1ms.
    t = _with_link_defaults(
        {"from": "relay-a", "to": "relay-b", "forward": {"delay_ms": 0.0}}
    )
    assert t.links[0].forward.delay_ms == 0.0


def test_explicit_null_clears_inherited_delay():
    # `null` is how a link opts out of an inherited qdisc entirely — distinct
    # from 0, which still yields `netem delay 0ms`. Bottleneck links rely on
    # this to keep netem out of the htb -> AQM chain.
    t = _with_link_defaults(
        {"from": "relay-a", "to": "relay-b", "forward": {"delay_ms": None}}
    )
    assert t.links[0].forward.delay_ms is None
    assert t.links[0].forward.is_noop()


def test_directions_inherit_independently():
    t = _with_link_defaults(
        {"from": "relay-a", "to": "relay-b", "forward": {"delay_ms": 9.0}}
    )
    assert t.links[0].forward.delay_ms == 9.0
    assert t.links[0].reverse.delay_ms == 1.0


def test_no_link_defaults_leaves_links_untouched():
    t = TopologyConfig.model_validate(
        {
            "relays": _minimal_relays(),
            "links": [{"from": "relay-a", "to": "relay-b"}],
        }
    )
    assert t.links[0].forward.is_noop()
    assert t.links[0].reverse.is_noop()


def test_inherited_aqm_still_policed_to_router_egress():
    # Resolution runs before validation, so a default-supplied aqm cannot
    # smuggle dualpi2 onto an endpoint's egress.
    raw = {
        "relays": _minimal_relays(),
        "defaults": {"link": {"forward": {"aqm": "dualpi2"}}},
        "links": [{"from": "relay-a", "to": "relay-b"}],
    }
    with pytest.raises(Exception, match="forward.aqm requires the egress node"):
        TopologyConfig.model_validate(raw)


def test_link_can_clear_inherited_aqm():
    raw = {
        "relays": _minimal_relays(),
        "routers": {"rt-1": {}},
        "defaults": {"link": {"forward": {"aqm": "dualpi2"}}},
        "links": [
            {"from": "rt-1", "to": "relay-a"},
            {"from": "relay-b", "to": "rt-1", "forward": {"aqm": None}},
        ],
    }
    t = TopologyConfig.model_validate(raw)
    assert t.links[0].forward.aqm is not None
    assert t.links[1].forward.aqm is None


# ── routers ────────────────────────────────────────────────────────────────


def test_aqm_on_endpoint_egress_rejected():
    raw = {
        "relays": _minimal_relays(),
        "links": [
            {"from": "relay-a", "to": "relay-b", "forward": {"aqm": "dualpi2"}},
        ],
    }
    with pytest.raises(Exception, match="forward.aqm requires the egress node"):
        TopologyConfig.model_validate(raw)


def test_aqm_on_router_egress_accepted_both_orientations():
    t = TopologyConfig.model_validate(
        {
            "relays": _minimal_relays(),
            "routers": {"rt-1": {}},
            "links": [
                {"from": "rt-1", "to": "relay-a", "forward": {"aqm": "dualpi2"}},
                {"from": "relay-b", "to": "rt-1", "reverse": {"aqm": "dualpi2"}},
            ],
        }
    )
    assert t.links[0].forward.aqm is not None
    assert t.links[1].reverse.aqm is not None


def test_aqm_toward_router_on_endpoint_egress_rejected():
    raw = {
        "relays": _minimal_relays(),
        "routers": {"rt-1": {}},
        "links": [
            {"from": "relay-a", "to": "rt-1", "forward": {"aqm": "dualpi2"}},
            {"from": "rt-1", "to": "relay-b"},
        ],
    }
    with pytest.raises(Exception, match="forward.aqm requires the egress node"):
        TopologyConfig.model_validate(raw)


def test_orphan_router_rejected():
    raw = {
        "relays": _minimal_relays(),
        "routers": {"rt-ghost": {}},
        "links": [{"from": "relay-a", "to": "relay-b"}],
    }
    with pytest.raises(Exception, match="does not appear in any link"):
        TopologyConfig.model_validate(raw)


def test_router_id_collides_with_relay_id():
    raw = {
        "relays": _minimal_relays(),
        "routers": {"relay-a": {}},
        "links": [{"from": "relay-a", "to": "relay-b"}],
    }
    with pytest.raises(Exception, match="reused"):
        TopologyConfig.model_validate(raw)


def test_upstream_cannot_reference_router():
    relays = _minimal_relays()
    relays["relay-b"]["upstream"] = "rt-1"
    raw = {
        "relays": relays,
        "routers": {"rt-1": {}},
        "links": [
            {"from": "relay-a", "to": "rt-1"},
            {"from": "rt-1", "to": "relay-b"},
        ],
    }
    with pytest.raises(Exception, match="not a known relay"):
        TopologyConfig.model_validate(raw)


def test_connects_to_cannot_reference_router():
    raw = {
        "relays": _minimal_relays(),
        "routers": {"rt-1": {}},
        "publishers": {"pub": {"connects_to": "rt-1", "namespace": "x"}},
        "links": [
            {"from": "relay-a", "to": "rt-1"},
            {"from": "rt-1", "to": "relay-b"},
        ],
    }
    with pytest.raises(Exception, match="not a known relay"):
        TopologyConfig.model_validate(raw)


def test_router_image_falls_back_to_defaults():
    t = TopologyConfig.model_validate(
        {
            "relays": _minimal_relays(),
            "routers": {"rt-1": {}, "rt-2": {"image": "custom-router"}},
            "links": [
                {"from": "relay-a", "to": "rt-1"},
                {"from": "rt-1", "to": "relay-b"},
                {"from": "relay-b", "to": "rt-2"},
            ],
        }
    )
    assert t.router_image("rt-1") == "moqlab-router"
    assert t.router_image("rt-2") == "custom-router"


# ── strictness ─────────────────────────────────────────────────────────────


def test_rejects_unknown_field_at_top_level():
    raw = {"relays": _minimal_relays(), "scenarios": []}
    with pytest.raises(Exception):
        TopologyConfig.model_validate(raw)


def test_rejects_unknown_field_on_relay():
    relays = _minimal_relays()
    relays["relay-a"]["mystery"] = 1
    with pytest.raises(Exception):
        TopologyConfig.model_validate({"relays": relays})


def test_rejects_generative_mode():
    raw = {"topology_mode": "generative", "relays": _minimal_relays()}
    with pytest.raises(Exception):
        TopologyConfig.model_validate(raw)


def test_tls_insecure_false_requires_cert():
    relays = _minimal_relays()
    relays["relay-a"]["tls"] = {"insecure": False}
    with pytest.raises(Exception, match="cert_file"):
        TopologyConfig.model_validate({"relays": relays})


def test_endpoint_must_start_with_slash():
    relays = _minimal_relays()
    relays["relay-a"]["endpoint"] = "moq-relay"
    with pytest.raises(Exception):
        TopologyConfig.model_validate({"relays": relays})


def test_pub_log_level_validated():
    raw = {
        "relays": _minimal_relays(),
        "publishers": {"pub": {"connects_to": "relay-a", "namespace": "x", "log_level": "TRACE"}},
    }
    with pytest.raises(Exception, match="log_level"):
        TopologyConfig.model_validate(raw)


# ── loader ─────────────────────────────────────────────────────────────────


def test_load_topology_file_not_found(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_topology(tmp_path / "nope.yaml")


def test_load_topology_empty_file(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    with pytest.raises(ConfigError, match="empty"):
        load_topology(p)
