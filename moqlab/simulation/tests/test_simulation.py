from __future__ import annotations

import json
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml
from pydantic import ValidationError

from simulation.analysis import confidence, ratios, truth_pairs, score_run
from simulation.designer import editor_config, editor_schema, make_server, parse_editor
from simulation.runner import ROOT
from simulation.schema import SimulationConfig, document, load_config, resolved_config


@pytest.fixture
def example():
    return load_config(ROOT / "examples/shared.yaml")


def test_document_round_trip_and_legacy_import(example, tmp_path):
    data = document(example)
    path = tmp_path / "simulation.yaml"
    path.write_text(yaml.safe_dump(data))
    assert document(load_config(path)) == data
    assert document(parse_editor(editor_config(example))) == data
    path.write_text(yaml.safe_dump(data["topology"]))
    assert load_config(path).topology.relays.keys() == example.topology.relays.keys()


def test_resolved_paths_preserve_switches_and_fanout(example):
    result = resolved_config(example)
    assert len(result["applications"]) == 3
    route = next(r for r in result["experiment"]["routes"] if r["receiver"] == "client-a")
    assert route["forward"] == ["server", "router", "switch", "client-a"]
    assert route["reverse"] == list(reversed(route["forward"]))


@pytest.mark.parametrize("edit", [
    lambda d: d["experiment"].update(duration_s=float("inf")),
    lambda d: d["topology"]["links"][0]["forward"].update(bandwidth_mbps=float("inf")),
    lambda d: d["experiment"].update(queues={"missing->switch": {}}),
    lambda d: d["experiment"].update(workloads={"missing": {}}),
    lambda d: d["experiment"].update(events=[{"kind": "flow", "time_s": 80, "origin": "origin", "active": False}]),
    lambda d: d["experiment"].update(events=[{"kind": "route", "time_s": 10, "sender": "server", "receiver": "client-a", "forward": ["server", "client-a"], "reverse": ["client-a", "server"]}]),
    lambda d: d["topology"]["relays"]["server"]["sbd"].update(algorithm="DcSBD"),
    lambda d: d["experiment"].update(quic_bulk=[{"id": "x", "sender": "server", "receiver": "server"}]),
    lambda d: (d["experiment"].update(link_mtu_bytes=1280), d["experiment"]["transport"].update(mtu_bytes=1400)),
])
def test_invalid_experiments_fail_before_launch(example, edit):
    data = document(example)
    edit(data)
    with pytest.raises(ValidationError):
        SimulationConfig.model_validate(data)


def test_ground_truth_overlap_and_unknown_accounting():
    value = {"flow_paths": {"a": ["x", "y"], "b": ["x"], "c": ["y"]}, "active_queues": ["y"]}
    pairs = list(truth_pairs(value))
    assert [p["structural"] for p in pairs] == [True, True, False]
    assert [p["active"] for p in pairs] == [False, True, False]
    result = ratios({"tp": 1, "fp": 0, "tn": 1, "fn": 0, "unknown": 2})
    assert result["coverage"] == .5 and result["precision"] == 1
    assert ratios({"tp": 0, "fp": 0, "tn": 0, "fn": 0})["precision"] is None
    assert confidence([.1, .9], 1000, 7) == confidence([.1, .9], 1000, 7)
    assert confidence([1], 1000, 7)["ci95"] is None


def test_observed_path_and_threshold_sensitivity(tmp_path):
    value = {"flow_paths": {"a": ["new-a"], "b": ["new-b"]},
             "observed_paths": {"a": ["old"], "b": ["old"]}, "active_queues": ["old"]}
    pair = next(truth_pairs(value))
    assert pair["active"] and not pair["structural"]
    (tmp_path / "COMPLETE").write_text("complete")
    (tmp_path / "events.jsonl").write_text("")
    (tmp_path / "truth.jsonl").write_text(json.dumps({"time_us": 350000, "server": "s", "window_us": 350000, **value}) + "\n")
    snapshot = {"time_us": 350000, "interval_end_us": 350000, "server": "s", "groups": [["a", "b"]],
                "flows": [{"id": "a", "known": True}, {"id": "b", "known": True}]}
    (tmp_path / "sbd.jsonl").write_text(json.dumps(snapshot) + "\n")
    assert score_run(tmp_path)["scores"]["evidence/active"]["counts"]["tp"] == 1
    assert score_run(tmp_path, {350000: set()})["scores"]["evidence/active"]["counts"]["fp"] == 1


def test_no_docker_import_path():
    code = "import sys; import simulation.__main__; assert not ({'docker','mininet','moqlab.cli'} & set(sys.modules))"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT.parent, check=True)


def test_latency_counts_missed_epochs_without_initial_negative_recovery(tmp_path):
    (tmp_path / "COMPLETE").write_text("complete")
    (tmp_path / "events.jsonl").write_text("")
    truth, snapshots = [], []
    for index, (active, known) in enumerate([(False, True), (True, False), (False, True)], 1):
        time = index * 350000
        truth.append({"time_us": time, "server": "s", "window_us": 350000,
                      "flow_paths": {"a": ["q"], "b": ["q"]}, "active_queues": ["q"] if active else []})
        snapshots.append({"time_us": time, "interval_end_us": time, "server": "s", "groups": [],
                          "flows": [{"id": name, "known": known} for name in ["a", "b"]]})
    (tmp_path / "truth.jsonl").write_text("\n".join(map(json.dumps, truth)) + "\n")
    (tmp_path / "sbd.jsonl").write_text("\n".join(map(json.dumps, snapshots)) + "\n")
    result = score_run(tmp_path)
    assert result["missed_completed_epochs"] == 1
    assert result["unresolved_transitions"] == 1
    assert result["latency"]["recovery_ms"]["samples"] == 1


def test_late_connection_warmup_counts_unknown_pairs(tmp_path):
    (tmp_path / "COMPLETE").write_text("complete")
    (tmp_path / "events.jsonl").write_text("")
    truth = [{"time_us": time, "server": "s", "window_us": 350000, "active_queues": [],
              "flow_paths": paths} for time, paths in [(350000, {"a": ["q"]}), (700000, {"a": ["q"], "b": ["q"]})]]
    (tmp_path / "truth.jsonl").write_text("\n".join(map(json.dumps, truth)) + "\n")
    snapshot = {"time_us": 700000, "interval_end_us": 0, "server": "s", "groups": [],
                "flows": [{"id": name, "known": False} for name in ["a", "b"]]}
    (tmp_path / "sbd.jsonl").write_text(json.dumps(snapshot) + "\n")
    assert score_run(tmp_path)["scores"]["evidence/active"]["counts"]["unknown"] == 1


def test_designer_schema_exposes_all_experiment_fields():
    schema, manifest = editor_schema()
    assert manifest["simulation"] and "experiment" in manifest["genericSections"]
    for field in ["transport", "workloads", "queues", "routes", "events", "quic_bulk", "seed", "repetitions", "evaluation"]:
        assert field in schema["properties"]["experiment"]["properties"]
    assert set(schema["$defs"]["Queue"]["properties"]["kind"]["enum"]) == {"fifo", "red", "pie", "fq_codel", "dualpi2"}
    assert schema["$defs"]["SbdConfig"]["properties"]["algorithm"]["enum"] == ["PracticalPassiveAndRFC"]


def test_designer_http_round_trip_and_invalid_requests(example):
    server = make_server(None, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        draft = editor_config(example)
        draft["experiment"]["events"] = [{"kind": "flow", "time_s": 12, "origin": "origin", "active": False}]
        request = Request(base + "/api/designer/export", json.dumps(draft).encode(), {"Content-Type": "application/json"})
        with urlopen(request) as response:
            exported = response.read()
        value = yaml.safe_load(exported)
        assert value["kind"] == "simulation" and value["experiment"]["events"][0]["time_s"] == 12
        request = Request(base + "/api/designer/import", exported, {"Content-Type": "application/yaml"})
        with urlopen(request) as response:
            imported = json.load(response)
        assert imported["config"]["experiment"] == draft["experiment"]
        request = Request(base + "/api/designer/validate", b'[]', {"Content-Type": "application/json"})
        with pytest.raises(HTTPError):
            urlopen(request)
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_all_bundled_examples_validate():
    for path in (ROOT / "examples").glob("*.yaml"):
        resolved_config(load_config(path))
