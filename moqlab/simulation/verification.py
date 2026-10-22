from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import yaml

from moqlab.exceptions import ConfigError
from simulation.analysis import analyze, rows
from simulation.runner import BINARY, BUILD, ROOT, execute, run
from simulation.schema import SimulationConfig, document, load_config


def scenarios() -> dict[str, SimulationConfig]:
    base = document(load_config(ROOT / "examples/shared.yaml"))
    cases = {}
    for name in ["shared", "uncongested", "private", "route-change"]:
        cases[name] = load_config(ROOT / f"examples/{name}.yaml")
    for controller in ["cubic", "bbr"]:
        for queue in ["fifo", "red", "pie", "fq_codel", "dualpi2"]:
            value = deepcopy(base)
            value["experiment"]["duration_s"] = 5
            value["experiment"]["transport"]["congestion_control"] = controller
            value["experiment"]["queue"]["kind"] = queue
            cases[f"{controller}-{queue}"] = SimulationConfig.model_validate(value)
    value = deepcopy(base)
    value["experiment"]["duration_s"] = 5
    value["experiment"]["transport"]["ecn"] = "ect1"
    value["experiment"]["queue"]["kind"] = "dualpi2"
    value["experiment"]["qlog"] = True
    cases["dualpi2-ect1"] = SimulationConfig.model_validate(value)
    value = deepcopy(base)
    value["experiment"]["duration_s"] = 5
    value["experiment"]["trace"] = "packets"
    value["experiment"]["pcap"] = True
    value["experiment"]["qlog"] = True
    cases["instrumentation"] = SimulationConfig.model_validate(value)
    value = deepcopy(base)
    value["experiment"]["duration_s"] = 6
    value["experiment"]["events"] = [
        {"kind": "flow", "time_s": 1, "origin": "origin", "active": False},
        {"kind": "queue", "time_s": 2, "source": "router", "target": "switch", "queue": {"kind": "red"}, "backlog_policy": "flush"},
        {"kind": "flow", "time_s": 3, "origin": "origin", "active": True},
        {"kind": "link", "time_s": 4, "source": "router", "target": "switch", "bandwidth_mbps": 6, "loss_pct": 1},
    ]
    cases["events"] = SimulationConfig.model_validate(value)
    value = deepcopy(base)
    value["experiment"]["duration_s"] = 6
    value["experiment"]["workload"]["kind"] = "segmented"
    cases["segmented"] = SimulationConfig.model_validate(value)
    value = deepcopy(base)
    value["experiment"]["duration_s"] = 6
    value["experiment"]["workload"]["kind"] = "bulk"
    value["experiment"]["quic_bulk"] = [{"id": "background", "sender": "server", "receiver": "client-b", "start_s": 1, "duration_s": 3}]
    cases["quic-bulk"] = SimulationConfig.model_validate(value)
    value = deepcopy(base)
    value["experiment"]["duration_s"] = 60
    value["topology"]["links"][3]["reverse"]["loss_pct"] = 3
    value["topology"]["links"][4]["reverse"]["bandwidth_mbps"] = .1
    value["topology"]["links"][4]["forward"]["jitter_ms"] = 2
    cases["feedback-impairment"] = SimulationConfig.model_validate(value)
    value = deepcopy(base)
    value["experiment"]["duration_s"] = 60
    value["experiment"]["connections"] = {"server->client-b": {"start_s": 5, "stop_s": 50}}
    cases["flow-churn"] = SimulationConfig.model_validate(value)
    value = deepcopy(base)
    value["topology"]["links"][4]["forward"]["bandwidth_mbps"] = 2
    cases["tandem"] = SimulationConfig.model_validate(value)
    value = deepcopy(base)
    value["experiment"]["duration_s"] = 60
    value["topology"]["switches"] = {}
    value["topology"]["routers"] = {name: {} for name in ["left", "middle", "right"]}
    value["topology"]["subscribers"]["client-c"] = deepcopy(value["topology"]["subscribers"]["client-a"])
    value["topology"]["links"] = [
        {"from": "origin", "to": "server"}, {"from": "server", "to": "left"},
        {"from": "left", "to": "middle", "forward": {"bandwidth_mbps": 6, "delay_ms": 5}},
        {"from": "server", "to": "middle"},
        {"from": "middle", "to": "right", "forward": {"bandwidth_mbps": 6, "delay_ms": 5}},
        {"from": "middle", "to": "client-a", "forward": {"delay_ms": 15}, "reverse": {"delay_ms": 15}},
        {"from": "right", "to": "client-b", "forward": {"delay_ms": 25}, "reverse": {"delay_ms": 25}},
        {"from": "right", "to": "client-c", "forward": {"delay_ms": 20}, "reverse": {"delay_ms": 20}}]
    value["experiment"]["routes"] = []
    for target, path in [("client-a", ["server", "left", "middle", "client-a"]),
                         ("client-b", ["server", "left", "middle", "right", "client-b"]),
                         ("client-c", ["server", "middle", "right", "client-c"])]:
        value["experiment"]["routes"].append({"sender": "server", "receiver": target, "forward": path, "reverse": list(reversed(path))})
    cases["overlapping-sharing"] = SimulationConfig.model_validate(value)
    value = deepcopy(base)
    value["topology"]["switches"] = {}
    value["topology"]["routers"] = {name: {} for name in ["router", "branch-a", "branch-b"]}
    value["topology"]["links"] = [{"from": "origin", "to": "server"}, {"from": "server", "to": "router"}]
    value["topology"]["traffic"] = {"senders": [], "receivers": [], "routes": {}, "flows": []}
    for suffix, delay in [("a", 15), ("b", 25)]:
        branch, client, tx, rx = (f"branch-{suffix}", f"client-{suffix}", f"tx-{suffix}", f"rx-{suffix}")
        value["topology"]["links"].extend([
            {"from": "router", "to": branch, "forward": {"bandwidth_mbps": 4}},
            {"from": branch, "to": client, "forward": {"delay_ms": delay}, "reverse": {"delay_ms": delay}},
            {"from": tx, "to": "router"}, {"from": branch, "to": rx}])
        traffic = value["topology"]["traffic"]
        traffic["senders"].append({"id": tx}); traffic["receivers"].append({"id": rx})
        traffic["routes"][suffix] = {"path": [tx, "router", branch, rx]}
        traffic["flows"].append({"id": suffix, "kind": "cbr", "route": suffix, "start_s": 10, "duration_s": 40, "rate_mbps": 3})
    cases["correlated-independent"] = SimulationConfig.model_validate(value)
    for kind in ["bulk", "cbr", "segmented"]:
        value = deepcopy(base)
        value["experiment"]["duration_s"] = 6
        value["topology"]["traffic"] = {
            "senders": [{"id": "traffic-tx"}], "receivers": [{"id": "traffic-rx"}],
            "routes": {"load": {"path": ["traffic-tx", "router", "switch", "traffic-rx"]}},
            "flows": [{"id": "load", "route": "load", "kind": kind, "start_s": 1, "duration_s": 3,
                       **({"rate_mbps": 5} if kind == "cbr" else {"representation_sequence_mbps": [5, 3]} if kind == "segmented" else {})}]}
        value["topology"]["links"].extend([{"from": "traffic-tx", "to": "router"}, {"from": "switch", "to": "traffic-rx"}])
        cases[f"traffic-{kind}"] = SimulationConfig.model_validate(value)
    return cases


def verify() -> Path:
    if not BINARY.exists():
        raise ConfigError("build simulation before verification")
    result = ROOT / ".runs" / ("verification-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
    result.mkdir(parents=True)
    report = {"status": "running", "checks": [], "cases": {}}
    path = result / "verification.json"
    try:
        execute(["ctest", "--test-dir", str(BUILD / "native"), "--output-on-failure"], log=result / "native-tests.log")
        report["checks"].append("native SBD independent calculations and analytic wire checks")
        execute([sys.executable, "-m", "pytest", "-q", str(ROOT / "tests" / "test_simulation.py")], cwd=ROOT.parent, log=result / "python-tests.log")
        report["checks"].append("Python schema, API, scoring and isolation tests")
        execute([str(BUILD / "pico-build/picoquic_ct"), "-S", str(BUILD / "picoquic"), "-n", "-r"], cwd=BUILD, log=result / "picoquic-tests.log")
        report["checks"].append("picoquic upstream tests")
        native_runner = BUILD / "ns3/build/utils/ns3.48-test-runner"
        for suite in ["dualpi2-queue-disc", "stratum-l4s", "red-queue-disc", "pie-queue-disc", "codel-queue-disc"]:
            execute([str(native_runner), f"--suite={suite}", "--verbose"], log=result / f"{suite}.log")
            report["checks"].append(f"ns-3 {suite}")
        cases = scenarios()
        cases["repeat-shared"] = cases["shared"]
        for name, config in cases.items():
            source = result / f"{name}.yaml"
            source.write_text(yaml.safe_dump(document(config), sort_keys=False))
            directory = run(config, source, result / name)
            complete = next(item for item in rows(directory / "run-0001/events.jsonl") if item["event"] == "simulation_complete")
            if not all(flow["received"] > 0 for flow in complete["flows"]):
                raise ConfigError(f"{name}: a configured flow received no bytes")
            analysis = analyze(directory)
            if name == "instrumentation":
                execute([str(BUILD / "native/sbd-replay"), str(directory / "run-0001/packets.jsonl"),
                         str(directory / "run-0001/sbd.jsonl")], log=result / "feedback-replay.log")
                if not list((directory / "run-0001/qlog").glob("*")):
                    raise ConfigError("requested qlog output is missing")
                if any(file.stat().st_size < 24 for file in (directory / "run-0001").glob("*.pcap")):
                    raise ConfigError("PCAP was not flushed")
            if name == "dualpi2-ect1":
                ce = 0
                for file in (directory / "run-0001/qlog").glob("*.server.qlog"):
                    for trace in json.loads(file.read_text())["traces"]:
                        for event in trace["events"]:
                            if event[2] == "packet_received":
                                ce = max(ce, *(int(frame.get("ce", 0)) for frame in event[3].get("frames", [])), 0)
                if not ce:
                    raise ConfigError("ECN test did not observe CE in sender-side QUIC ACK feedback")
                report["checks"].append("CE marking reaches sender through QUIC ACK_ECN")
            report["cases"][name] = {"status": "passed", "analysis": str(analysis)}
            path.write_text(json.dumps(report, indent=2) + "\n")
        for artifact in ["sbd.jsonl", "applications.jsonl", "queues.jsonl", "truth.jsonl"]:
            if (result / "shared/run-0001" / artifact).read_bytes() != (result / "repeat-shared/run-0001" / artifact).read_bytes():
                raise ConfigError(f"same-seed semantic reproducibility failed: {artifact}")
        report["checks"].append("identical same-seed semantic traces")
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error)
        raise
    finally:
        path.write_text(json.dumps(report, indent=2) + "\n")
    return path
