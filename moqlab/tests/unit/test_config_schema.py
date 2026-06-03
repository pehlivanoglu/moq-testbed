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
    assert len(t.links) == 4
    assert t.startup.publisher_warmup_s == 1.0
    assert t.relays["relay-c"].upstream == "relay-b"
    assert t.publishers["pub"].connects_to == "relay-a"
    assert t.subscribers["sub"].connects_to == "relay-c"


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


def test_link_for_lookup_is_endpoint_order_agnostic():
    t = TopologyConfig.model_validate(
        {
            "relays": _minimal_relays(),
            "links": [{"from": "relay-a", "to": "relay-b", "delay_ms": 5.0}],
        }
    )
    link = t.link_for("relay-b", "relay-a")
    assert link is not None
    assert link.delay_ms == 5.0


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
