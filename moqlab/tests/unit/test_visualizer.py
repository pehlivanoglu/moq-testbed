from __future__ import annotations

from pathlib import Path

from moqlab.config.schema import TopologyConfig
from moqlab.visualizer import (
    InterfaceCounters,
    ThroughputSampler,
    _VisualizerHandler,
    VisualizerHTTPServer,
    parse_node_metrics,
    topology_snapshot,
)


def test_browser_assets_live_outside_python_package():
    root = Path(__file__).resolve().parents[2] / "visualizer"

    assert (root / "index.html").is_file()
    assert (root / "style.css").is_file()
    assert (root / "app.js").is_file()

    html = (root / "index.html").read_text()
    app = (root / "app.js").read_text()
    assert 'id="node-details"' in html
    assert "/api/nodes/${encodeURIComponent(requestedId)}/metrics" in app
    assert 'classList.toggle("selected"' in app
    assert "function updateNodeMetrics(payload)" in app
    assert "updateNodeMetrics(await response.json())" in app
    assert app.count("nodeDetails.replaceChildren()") == 1


def test_response_write_ignores_broken_pipe():
    class BrokenPipeWriter:
        def write(self, _body: bytes) -> None:
            raise BrokenPipeError

    handler = object.__new__(_VisualizerHandler)
    handler.wfile = BrokenPipeWriter()
    handler.send_response = lambda *_args, **_kwargs: None
    handler.send_header = lambda *_args, **_kwargs: None
    handler.end_headers = lambda: None

    handler._send_json({"ok": True})


def _topology() -> TopologyConfig:
    return TopologyConfig.model_validate(
        {
            "relays": {
                "relay-a": {"listen_port": 9668, "admin_port": 9669},
            },
            "publishers": {
                "pub": {"connects_to": "relay-a"},
            },
            "subscribers": {
                "sub": {
                    "connects_to": "relay-a",
                    "namespace": "msf/clear",
                    "track": "video/s2",
                }
            },
            "links": [
                {"from": "pub", "to": "relay-a", "forward": {"bandwidth_mbps": 100}},
                {"from": "relay-a", "to": "sub", "forward": {"delay_ms": 5}},
            ],
        }
    )


def _unshaped(**overrides) -> dict:
    payload = {
        "bandwidth_mbps": None,
        "delay_ms": None,
        "jitter_ms": None,
        "loss_pct": None,
        "aqm": None,
    }
    payload.update(overrides)
    return payload


def test_topology_snapshot_includes_directional_links():
    snapshot = topology_snapshot(_topology())

    assert snapshot["summary"] == {
        "relays": 1,
        "routers": 0,
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
            "forward": _unshaped(bandwidth_mbps=100.0),
            "reverse": _unshaped(),
        },
        {
            "id": "relay-a--sub",
            "source": "relay-a",
            "target": "sub",
            "forward": _unshaped(delay_ms=5.0),
            "reverse": _unshaped(),
        },
    ]


def test_topology_snapshot_places_router_between_endpoints():
    topology = TopologyConfig.model_validate(
        {
            "relays": {"relay-a": {"listen_port": 9668, "admin_port": 9669}},
            "publishers": {"pub": {"connects_to": "relay-a"}},
            "subscribers": {
                "sub": {"connects_to": "relay-a", "namespace": "n", "track": "t"}
            },
            "routers": {"rt-1": {}},
            "links": [
                {"from": "pub", "to": "relay-a"},
                {"from": "relay-a", "to": "rt-1"},
                {
                    "from": "rt-1",
                    "to": "sub",
                    "forward": {"bandwidth_mbps": 20, "aqm": "dualpi2"},
                },
            ],
        }
    )

    snapshot = topology_snapshot(topology)

    assert snapshot["summary"]["routers"] == 1
    levels = {node["id"]: node["level"] for node in snapshot["nodes"]}
    roles = {node["id"]: node["role"] for node in snapshot["nodes"]}
    assert roles["rt-1"] == "router"
    assert levels["pub"] < levels["relay-a"] < levels["rt-1"] < levels["sub"]
    aqm_link = next(l for l in snapshot["links"] if l["id"] == "rt-1--sub")
    assert aqm_link["forward"]["aqm"] == "dualpi2"


def test_media_snapshot_includes_x11_browser_mode():
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
                    "track": "video/s2", "browser_mode": "x11",
                }
            },
        }
    )

    snapshot = topology_snapshot(topology)
    sub = next(node for node in snapshot["nodes"] if node["id"] == "sub")
    assert sub["kind"] == "media"
    assert sub["media_client"] == "chrome"
    assert sub["browser_mode"] == "x11"


def test_native_media_snapshot_has_no_browser_mode():
    topology = TopologyConfig.model_validate(
        {
            "defaults": {
                "relay": {"tls": {"insecure": False, "generated": True}},
                "subscriber": {"media_client": "native"},
            },
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

    sub = next(node for node in topology_snapshot(topology)["nodes"] if node["id"] == "sub")

    assert sub["media_client"] == "native"
    assert sub["browser_mode"] is None
    assert sub["native_playback"] == "receive"


def test_node_metrics_resolves_containernet_name_and_staleness():
    topology = TopologyConfig.model_validate(
        {
            "defaults": {
                "relay": {"tls": {"insecure": False, "generated": True}},
                "subscriber": {
                    "media_client": "native",
                    "native_playback": "simulate",
                },
            },
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
    seen = []
    server = VisualizerHTTPServer(
        ("127.0.0.1", 0), topology,
        metrics_reader=lambda container: seen.append(container) or (
            b'{"schema_version":1,"sampled_at_unix_ms":1000}'
        ),
    )
    try:
        result = server.node_metrics("sub")
    finally:
        server.server_close()

    assert seen == ["mn.sub"]
    assert result["status"] == "stale"


def test_node_metrics_handles_unregistered_and_unknown_nodes():
    server = VisualizerHTTPServer(
        ("127.0.0.1", 0), _topology(), backend="docker", metrics_reader=lambda _id: None
    )
    try:
        assert server.node_metrics("sub")["reason"] == "container not registered"
        assert server.node_metrics("missing")["reason"] == "unknown node"
    finally:
        server.server_close()


def test_node_metrics_uses_registered_docker_container_id():
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
                    "kind": "media", "connects_to": "root",
                    "namespace": "msf/clear", "track": "video/s2",
                }
            },
        }
    )
    seen = []
    server = VisualizerHTTPServer(
        ("127.0.0.1", 0), topology, backend="docker",
        metrics_reader=lambda container: seen.append(container) or (
            b'{"schema_version":1,"sampled_at_unix_ms":9999999999999}'
        ),
    )
    try:
        assert server.node_metrics("sub")["status"] == "unavailable"
        server.register_subscriber_containers({"sub": "abc123"})
        assert server.node_metrics("sub")["status"] == "ok"
    finally:
        server.server_close()

    assert seen == ["abc123"]


def test_parse_node_metrics_rejects_bad_json_and_marks_fresh_data_ok():
    assert parse_node_metrics(b"not json")["status"] == "unavailable"
    result = parse_node_metrics(
        b'{"schema_version":1,"sampled_at_unix_ms":9000}', now_unix_ms=10_000
    )
    assert result["status"] == "ok"


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
