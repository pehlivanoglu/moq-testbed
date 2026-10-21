from __future__ import annotations

from pathlib import Path
import json

import pytest

from moqlab.config.schema import TopologyConfig, load_topology
from moqlab.config.synth import synthesize_subscriber_command, synthesize_relay_yaml
from moqlab.runtime import node_loopback_ips
from moqlab.orchestrator.containernet_backend import ContainernetBackend, ContainernetRunRecord
from moqlab.rtt import plot_rtt, read_rtt, read_experiment_events


EXAMPLE = Path(__file__).resolve().parents[2] / "configs/examples/shared_bottleneck_different_rtt.yaml"


def test_rtt_config_synthesis_and_container_mounts(tmp_path: Path) -> None:
    from test_containernet_backend import _FakeBuildNet

    topology = load_topology(EXAMPLE)
    record = ContainernetRunRecord(run_id="rtt-test", run_dir=tmp_path)
    net = _FakeBuildNet()
    ContainernetBackend(tmp_path / "runs")._build(
        net, topology, {"relay": tmp_path / "relay.yaml"}, record, lambda _: None
    )
    outputs = synthesize_relay_yaml(topology, "relay")["rtt_outputs"]
    assert outputs == {
        node_loopback_ips(topology)[sid]: f"/var/log/moqlab/rtt/{sid}"
        for sid, subscriber in topology.subscribers.items() if subscriber.rtt_metrics
    }
    assert f"{tmp_path / 'rtt'}:/var/log/moqlab/rtt:rw" in net.docker_nodes["relay"]["volumes"]
    for sid in ("sub-fast", "sub-slow"):
        argv = synthesize_subscriber_command(topology, sid)
        assert "-rtt-path" not in argv
        directory = tmp_path / "rtt" / sid
        assert directory.is_dir()
        assert "volumes" not in net.docker_nodes[sid]
    for subscriber in topology.subscribers.values():
        subscriber.rtt_metrics = False
    assert "rtt_outputs" not in synthesize_relay_yaml(topology, "relay")


def test_relay_rtt_supports_browser_clients() -> None:
    raw = load_topology(EXAMPLE).model_dump()
    raw["subscribers"]["sub-fast"]["media_client"] = "chrome-headless"
    raw["subscribers"]["sub-fast"].pop("native_playback")
    assert TopologyConfig.model_validate(raw).subscribers["sub-fast"].rtt_metrics


def test_rtt_plot_aligns_clients_and_preserves_actual_intervals(tmp_path: Path, monkeypatch) -> None:
    plt = pytest.importorskip("matplotlib.pyplot")
    header = "timestamp_unix_ns,elapsed_ns,interval_ns,latest_rtt_ms,srtt_ms,min_rtt_ms,measurement_side\n"
    for client, timestamp, rtt in (("sub-fast", 1_000_000_000, 20), ("sub-slow", 1_020_000_000, 80)):
        directory = tmp_path / client
        directory.mkdir()
        (directory / "rtt.csv").write_text(
            header + f"{timestamp},1000000,1000000,{rtt},{rtt},19,relay\n"
            # Wall-clock jump: the plotted interval must still use monotonic time.
            + f"{timestamp + 9_000_000_000},8000000,7000000,{rtt + 5},{rtt + 1},19,relay\n"
        )
    figures = []
    original = plt.subplots

    def capture(*args, **kwargs):
        figure, axes = original(*args, **kwargs)
        figures.append((figure, axes))
        return figure, axes

    monkeypatch.setattr(plt, "subplots", capture)
    output = plot_rtt(tmp_path)
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    axes = figures[0][1]
    assert list(axes[0].lines[0].get_xdata()) == pytest.approx([0, 0.007])
    assert list(axes[0].lines[1].get_xdata()) == pytest.approx([0.02, 0.027])
    assert list(axes[0].lines[1].get_ydata()) == [80, 85]
    assert list(axes[2].lines[0].get_ydata()) == [1, 7]


def test_rtt_reader_rejects_empty_or_incomplete_data(tmp_path: Path) -> None:
    path = tmp_path / "rtt.csv"
    path.write_text("timestamp_unix_ns,elapsed_ns,interval_ns,latest_rtt_ms,srtt_ms,min_rtt_ms\n")
    with pytest.raises(ValueError, match="no RTT samples"):
        read_rtt(path)
    path.write_text(path.read_text() + "123,456\n")
    with pytest.raises((TypeError, ValueError)):
        read_rtt(path)


def test_rtt_reader_does_not_relabel_old_client_samples(tmp_path: Path) -> None:
    path = tmp_path / "rtt.csv"
    path.write_text(
        "timestamp_unix_ns,elapsed_ns,interval_ns,latest_rtt_ms,srtt_ms,min_rtt_ms\n"
        "1000000,1000000,1000000,20,20,20\n"
    )
    with pytest.raises(ValueError, match="not a relay-side RTT archive"):
        read_rtt(path)


def test_rtt_graph_generated_after_network_stops(tmp_path: Path, monkeypatch) -> None:
    import moqlab.orchestrator.containernet_backend as backend_module
    import moqlab.rtt as rtt_module

    events = []

    class Net:
        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    def cli(_net):
        events.append("cli")

    monkeypatch.setattr(backend_module, "_import_mininet", lambda: (cli, lambda _: None, lambda _: None, lambda **_: Net()))
    for name in ("generate_run_tls", "_remove_stale_mn_containers", "_modprobe_aqm_modules", "_write_etc_hosts_via_docker"):
        monkeypatch.setattr(backend_module, name, lambda *args: None)
    monkeypatch.setattr(backend_module, "synthesize_relay_configs", lambda *args: {})
    for name in ("_build", "_configure_network", "_sanity_ping", "_launch_node_binaries"):
        monkeypatch.setattr(ContainernetBackend, name, lambda *args: None)
    monkeypatch.setattr(rtt_module, "check_plotting", lambda: None)
    monkeypatch.setattr("moqlab.tc_events.TcEventMonitors.start", lambda self: events.append("monitor-start"))
    monkeypatch.setattr("moqlab.tc_events.TcEventMonitors.stop", lambda self: events.append("monitor-stop"))
    monkeypatch.setattr(backend_module, "_stop_traffic_sender", lambda *args: events.append("traffic-stop"))

    def plot(directory):
        assert events[-1] == "stop"
        events.append("plot")
        return directory / "rtt.png"

    monkeypatch.setattr(rtt_module, "plot_rtt", plot)
    ContainernetBackend(tmp_path).up(EXAMPLE, run_id="rtt-test")
    expected = ["start", "monitor-start", "cli"]
    if load_topology(EXAMPLE).traffic is not None:
        expected.append("traffic-stop")
    assert events == expected + ["monitor-stop", "stop", "plot"]


def test_plot_marks_multiple_egresses_and_real_traffic_on_monotonic_axis(tmp_path: Path, monkeypatch) -> None:
    plt = pytest.importorskip("matplotlib.pyplot")
    archive = tmp_path / "rtt"
    (archive / "sub-fast").mkdir(parents=True)
    (archive / "events").mkdir()
    (archive / "traffic").mkdir()
    (archive / "sub-fast/abc.csv").write_text(
        "timestamp_unix_ns,elapsed_ns,interval_ns,latest_rtt_ms,srtt_ms,min_rtt_ms,measurement_side,timestamp_mono_ns\n"
        "1000000000,1000000,1000000,20,20,20,relay,5000000000\n"
        "1007000000,8000000,7000000,30,25,20,relay,5007000000\n"
    )
    changes = []
    for node, interface, second in (("west", "west-eth1", 0.002), ("east", "east-eth2", 0.004)):
        changes.append({"event": "bandwidth_change", "node": node, "interface": interface,
                        "object": "class", "handle": "5:1", "kind": "htb",
                        "before": {"rate_bps": 100_000_000, "ceil_bps": 100_000_000},
                        "after": {"rate_bps": 10_000_000, "ceil_bps": 10_000_000},
                        "timestamp_unix_ns": 99_000_000_000,  # Simulated wall-clock step must not shift markers.
                        "timestamp_mono_ns": 5_000_000_000 + round(second * 1e9)})
    (archive / "events/changes.jsonl").write_text("".join(json.dumps(event) + "\n" for event in changes))
    (archive / "events/egresses.json").write_text(json.dumps({"west": {"west-eth1": "switch"}, "east": {"east-eth2": "sub-slow"}}))
    traffic = [
        {"event": "flow_start", "flow_id": "load", "kind": "cbr", "route": "shared",
         "timestamp_unix_ns": 99_000_000_000, "timestamp_mono_ns": 5_001_000_000},
        {"event": "flow_end", "flow_id": "load", "status": "completed",
         "timestamp_unix_ns": 99_000_000_000, "timestamp_mono_ns": 5_006_000_000},
    ]
    (archive / "traffic/tx.jsonl").write_text("".join(json.dumps(event) + "\n" for event in traffic))
    (tmp_path / "traffic-plan.json").write_text(json.dumps({
        "flows": [{"id": "load", "route": "shared"}, {"id": "never-started", "start_s": 1000}],
        "routes": {"shared": {"path": ["tx", "west", "switch", "rx"]}},
    }))
    figures = []
    original = plt.subplots

    def capture(*args, **kwargs):
        figure, axes = original(*args, **kwargs)
        figures.append((figure, axes))
        return figure, axes

    monkeypatch.setattr(plt, "subplots", capture)
    assert plot_rtt(archive).read_bytes().startswith(b"\x89PNG")
    axes = figures[0][1]
    assert [line.get_xdata()[0] for line in axes[0].lines[1:]] == pytest.approx([0.001, 0.002, 0.004, 0.006])
    text = " ".join(cell.get_text().get_text() for table in axes[3].tables for cell in table.get_celld().values())
    assert "west/west-eth1 → switch" in text
    assert "east/east-eth2 → sub-slow" in text
    assert "100 Mbit/s → 10 Mbit/s" in text
    assert "START (cbr, route=shared)" in text
    assert "END (completed)" in text
    assert "never-started" not in text


def test_unfinished_traffic_is_marked_interrupted_without_inventing_scheduled_end(tmp_path: Path) -> None:
    archive = tmp_path / "rtt"
    (archive / "traffic").mkdir(parents=True)
    start = {"event": "flow_start", "flow_id": "long", "kind": "bulk", "route": "main",
             "timestamp_unix_ns": 1_000, "timestamp_mono_ns": 100}
    (archive / "traffic/tx.jsonl").write_text(json.dumps(start) + "\n")
    (archive / "run-end.json").write_text(json.dumps({"timestamp_unix_ns": 3_000, "timestamp_mono_ns": 300}))
    events = read_experiment_events(archive)
    assert len(events) == 2
    assert "interrupted" in events[1]["label"]
    assert events[1]["timestamp_mono_ns"] == 300
    assert events[1]["start"]["timestamp_mono_ns"] == 100
