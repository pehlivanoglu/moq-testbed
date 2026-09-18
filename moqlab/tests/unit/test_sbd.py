from __future__ import annotations

import json
import time

import pytest
from pydantic import ValidationError

from moqlab.config.schema import RelayConfig, TopologyConfig
from moqlab.config.synth import synthesize_relay_yaml
from moqlab.visualizer import parse_sbd_metrics


def test_sbd_requires_explicit_edge_and_valid_mode():
    with pytest.raises(ValidationError, match="edge"):
        RelayConfig(listen_port=4443, admin_port=4444, sbd={"enabled": True})
    with pytest.raises(ValidationError):
        RelayConfig(listen_port=4443, admin_port=4444, edge=True,
                    sbd={"enabled": True, "delay_source": "automatic"})


@pytest.mark.parametrize("mode", ["owd", "rtt"])
def test_sbd_config_reaches_relay_and_archive(mode):
    topology = TopologyConfig.model_validate({
        "relays": {"edge": {"listen_port": 4443, "admin_port": 4444,
                            "edge": True, "sbd": {"enabled": True, "delay_source": mode}}},
        "publishers": {"pub": {"connects_to": "edge"}},
        "subscribers": {"sub": {"connects_to": "edge", "namespace": "msf/clear", "track": "video/s2"}},
    })
    doc = synthesize_relay_yaml(topology, "edge")
    assert doc["edge"]
    assert doc["sbd"] == {"enabled": True, "delay_source": mode,
                          "output_file": "/var/log/moqx/sbd/snapshots.jsonl"}


def test_sbd_live_stale_and_invalid_snapshots():
    assert parse_sbd_metrics(None)["status"] == "unavailable"
    assert parse_sbd_metrics(b"[]")["status"] == "unavailable"
    assert parse_sbd_metrics(b"{")["status"] == "unavailable"
    snapshot = {"timestamp_ms": time.time() * 1000, "clients": []}
    assert parse_sbd_metrics(json.dumps(snapshot).encode())["status"] == "live"
    snapshot["timestamp_ms"] -= 4000
    assert parse_sbd_metrics(json.dumps(snapshot).encode())["status"] == "stale"
