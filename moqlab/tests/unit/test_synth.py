"""Unit tests for moqlab.config.synth.

We don't byte-compare against the repo-root relay-a/b/c.yaml fixtures: those use
`moqt://localhost:<port>` URLs because they were authored for host-mode runs.
Our orchestrator uses container DNS names, so URLs differ by design.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from moqlab.config.schema import TopologyConfig
from moqlab.config.synth import (
    synthesize_publisher_command,
    synthesize_relay_configs,
    synthesize_relay_yaml,
    synthesize_subscriber_command,
)


def _topology() -> TopologyConfig:
    return TopologyConfig.model_validate(
        {
            "relays": {
                "relay-a": {"listen_port": 9668, "admin_port": 9669, "upstream": None},
                "relay-b": {"listen_port": 9670, "admin_port": 9671, "upstream": "relay-a"},
                "relay-c": {"listen_port": 9672, "admin_port": 9673, "upstream": "relay-b"},
            },
            "publishers": {
                "pub": {"connects_to": "relay-a", "namespace": "moq-date"},
            },
            "subscribers": {
                "sub": {"connects_to": "relay-c", "namespace": "moq-date", "track": "date"},
            },
        }
    )


# ── relay yaml ─────────────────────────────────────────────────────────────


def test_origin_relay_has_no_upstream():
    doc = synthesize_relay_yaml(_topology(), "relay-a")
    assert doc["relay_id"] == "relay-a"
    assert "upstream" not in doc["services"]["default"]
    listener = doc["listeners"][0]
    assert listener["udp"]["socket"]["port"] == 9668
    assert listener["endpoint"] == "/moq-relay"
    assert doc["admin"]["port"] == 9669


def test_downstream_relay_uses_container_dns():
    doc = synthesize_relay_yaml(_topology(), "relay-b")
    up = doc["services"]["default"]["upstream"]
    assert up["url"] == "moqt://relay-a:9668/moq-relay"
    assert up["tls"]["insecure"] is True


def test_chain_compounds():
    doc = synthesize_relay_yaml(_topology(), "relay-c")
    assert doc["services"]["default"]["upstream"]["url"] == "moqt://relay-b:9670/moq-relay"


def test_relay_overrides_supersede_defaults():
    t = TopologyConfig.model_validate(
        {
            "defaults": {"relay": {"endpoint": "/moq-relay", "cache": {"max_tracks": 100}}},
            "relays": {
                "r1": {
                    "listen_port": 9668,
                    "admin_port": 9669,
                    "upstream": None,
                    "endpoint": "/other",
                    "cache": {"enabled": True, "max_tracks": 999, "max_groups_per_track": 50},
                }
            },
        }
    )
    doc = synthesize_relay_yaml(t, "r1")
    assert doc["listeners"][0]["endpoint"] == "/other"
    assert doc["services"]["default"]["cache"]["max_tracks"] == 999


def test_synthesize_relay_configs_writes_files(tmp_path: Path):
    t = _topology()
    written = synthesize_relay_configs(t, tmp_path)
    assert set(written.keys()) == {"relay-a", "relay-b", "relay-c"}
    for rid, path in written.items():
        assert path.exists()
        loaded = yaml.safe_load(path.read_text())
        assert loaded["relay_id"] == rid


# ── publisher argv ─────────────────────────────────────────────────────────


def test_publisher_command_basic_shape():
    argv = synthesize_publisher_command(_topology(), "pub")
    assert "--ns=moq-date" in argv
    assert "--relay_url=https://relay-a:9668/moq-relay" in argv
    assert "--logging=INFO" in argv
    assert "--insecure" in argv
    # No --port flag because publisher.port was unset
    assert not any(a.startswith("--port=") for a in argv)


def test_publisher_command_honors_port_override():
    t = TopologyConfig.model_validate(
        {
            "relays": {"r": {"listen_port": 9668, "admin_port": 9669, "upstream": None}},
            "publishers": {"p": {"connects_to": "r", "namespace": "n", "port": 4500}},
        }
    )
    argv = synthesize_publisher_command(t, "p")
    assert "--port=4500" in argv


def test_publisher_command_drops_insecure_when_false():
    t = TopologyConfig.model_validate(
        {
            "relays": {"r": {"listen_port": 9668, "admin_port": 9669, "upstream": None}},
            "publishers": {"p": {"connects_to": "r", "namespace": "n", "insecure": False}},
        }
    )
    argv = synthesize_publisher_command(t, "p")
    assert "--insecure" not in argv


# ── subscriber argv ────────────────────────────────────────────────────────


def test_subscriber_command_basic_shape():
    argv = synthesize_subscriber_command(_topology(), "sub")
    assert "--connect_url=https://relay-c:9672/moq-relay" in argv
    assert "--track_namespace=moq-date" in argv
    assert "--track_name=date" in argv
    assert "--logging=INFO" in argv
    assert "--insecure" in argv


def test_subscriber_command_uses_per_relay_endpoint_override():
    t = TopologyConfig.model_validate(
        {
            "relays": {
                "r": {
                    "listen_port": 9668,
                    "admin_port": 9669,
                    "upstream": None,
                    "endpoint": "/custom",
                }
            },
            "subscribers": {"s": {"connects_to": "r", "namespace": "n", "track": "t"}},
        }
    )
    argv = synthesize_subscriber_command(t, "s")
    assert "--connect_url=https://r:9668/custom" in argv


def _media_topology() -> TopologyConfig:
    return TopologyConfig.model_validate(
        {
            "defaults": {"relay": {"tls": {"insecure": False, "generated": True}}},
            "relays": {
                "root": {"listen_port": 9668, "admin_port": 9669},
                "leaf": {"listen_port": 9670, "admin_port": 9671, "upstream": "root"},
            },
            "publishers": {
                "origin": {
                    "kind": "media", "connects_to": "root", "asset": "testsvc",
                    "listen_port": 4443, "fingerprint_port": 8081,
                }
            },
            "subscribers": {
                "viewer": {
                    "kind": "media", "connects_to": "leaf", "namespace": "msf/clear",
                    "track": "video/s2", "minimal_buffer_ms": 200,
                    "target_latency_ms": 300,
                }
            },
        }
    )


def test_media_root_pulls_origin_and_uses_exact_endpoint():
    topology = _media_topology()
    root = synthesize_relay_yaml(topology, "root")
    assert root["services"]["default"]["match"][0]["path"] == {"exact": "/moq-relay"}
    assert root["services"]["default"]["upstream"] == {
        "url": "moqt://origin:4443/moq", "tls": {"insecure": True}
    }
    assert root["listeners"][0]["tls"]["cert_file"] == "/run/moqlab/tls/cert.pem"


def test_media_leaf_uses_generated_tls_compatible_upstream():
    leaf = synthesize_relay_yaml(_media_topology(), "leaf")
    assert leaf["services"]["default"]["upstream"]["tls"] == {"insecure": True}


def test_media_commands_include_asset_namespace_track_and_buffers():
    topology = _media_topology()
    publisher = synthesize_publisher_command(topology, "origin")
    assert publisher[:4] == ["-addr", "0.0.0.0:4443", "-asset", "/opt/moqlivemock/assets/testsvc"]
    assert ["-sideport", "8081"] == publisher[8:10]
    assert ["-catalog-delay", "2s"] == publisher[10:12]
    subscriber = synthesize_subscriber_command(topology, "viewer")
    assert "--server-url=https://leaf:9670/moq-relay" in subscriber
    assert "--fingerprint-url=http://origin:8081/fingerprint" in subscriber
    assert "--namespace=msf/clear" in subscriber
    assert "--video-track=video/s2" in subscriber
    assert "--browser-mode=headless" in subscriber


def test_native_media_subscriber_command_uses_mlmsub_flags():
    topology = _media_topology()
    subscriber = topology.subscribers["viewer"]
    subscriber.media_client = "native"
    subscriber.browser_mode = None
    subscriber.minimal_buffer_ms = None
    subscriber.target_latency_ms = None

    argv = synthesize_subscriber_command(topology, "viewer")

    assert argv == [
        "-addr", "leaf:9670",
        "-draft", "16",
        "-namespace", "msf/clear",
        "-videoname", "video/s2",
        "-catalog-mode", "subscribe",
        "-subscribe-dependencies",
        "-loglevel", "info",
        "-qlog", "/tmp/viewer.qlog",
    ]
    leaf = synthesize_relay_yaml(topology, "leaf")
    assert {"authority": {"any": True}, "path": {"exact": "/"}} in (
        leaf["services"]["default"]["match"]
    )
