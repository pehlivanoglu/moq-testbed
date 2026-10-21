from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from moqlab.exceptions import OrchestratorError


def check_plotting() -> None:
    try:
        import matplotlib  # noqa: F401
    except ImportError as error:
        raise OrchestratorError(
            'rtt_metrics needs matplotlib in the run Python environment; '
            'install with: python -m pip install "matplotlib>=3.8,<4"'
        ) from error


def read_rtt(path: Path) -> list[dict[str, int | float]]:
    rows = []
    with path.open(newline="") as source:
        for row in csv.DictReader(source):
            if row.get("measurement_side") != "relay":
                raise ValueError(f"{path}: not a relay-side RTT archive; start a new run")
            rows.append({
                key: int(row[key]) if key.endswith("_ns") else float(row[key])
                for key in (
                    "timestamp_unix_ns", "elapsed_ns", "interval_ns",
                    "latest_rtt_ms", "srtt_ms", "min_rtt_ms",
                )
            })
            if row.get("timestamp_mono_ns"):
                rows[-1]["timestamp_mono_ns"] = int(row["timestamp_mono_ns"])
    if not rows:
        raise ValueError(f"{path}: no RTT samples")
    return rows


def read_experiment_events(directory: Path) -> list[dict]:
    events = []
    egress_path = directory / "events" / "egresses.json"
    egresses = json.loads(egress_path.read_text()) if egress_path.exists() else {}
    for path in sorted((directory / "events").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event["event"] == "bandwidth_initial":
                continue
            if event["event"] == "bandwidth_change":
                device = f"{event['node']}/{event['interface']}"
                peer = egresses.get(event["node"], {}).get(event["interface"])
                if peer:
                    device += f" → {peer}"
                device += f" [{event['object']} {event['handle']}]"
                changes = []
                for field in sorted(set(event["before"]) | set(event["after"])):
                    before, after = event["before"].get(field), event["after"].get(field)
                    if before == after:
                        continue
                    def rate(value):
                        return "unlimited/unset" if not value else f"{value / 1e6:g} Mbit/s"
                    changes.append(f"{field.removesuffix('_bps')}: {rate(before)} → {rate(after)}")
                event.update(label="; ".join(changes), device=device, color="tab:orange")
            elif event["event"] == "monitor_error":
                event.update(label=f"INCOMPLETE bandwidth history: {event['error']}",
                             device=event["node"], color="darkred")
            else:
                continue
            events.append(event)

    plan_path = directory.parent / "traffic-plan.json"
    plan = json.loads(plan_path.read_text()) if plan_path.exists() else {}
    flows = {flow["id"]: flow for flow in plan.get("flows", [])}
    active = {}
    for path in sorted((directory / "traffic").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event["event"] not in ("flow_start", "flow_end"):
                continue
            flow_id = event["flow_id"]
            flow = flows.get(flow_id, {})
            route = event.get("route", flow.get("route", "?"))
            path_nodes = plan.get("routes", {}).get(route, {}).get("path", [])
            device = " → ".join(path_nodes) or event.get("node", "traffic sender")
            kind = event.get("kind", flow.get("kind", "?"))
            start = event["event"] == "flow_start"
            label = f"External traffic {flow_id}: " + (f"START ({kind}, route={route})" if start
                                                      else f"END ({event.get('status', 'completed')})")
            event.update(label=label, device=device, color="tab:green" if start else "tab:red")
            if start:
                active[flow_id] = event
            else:
                previous = active.pop(flow_id, None)
                if previous is not None:
                    event["start"] = previous
            events.append(event)
    end_path = directory / "run-end.json"
    if active and end_path.exists():
        end = json.loads(end_path.read_text())
        for flow_id, start in active.items():
            events.append({**end, "event": "flow_end", "start": start,
                           "device": start["device"], "color": "tab:red",
                           "label": f"External traffic {flow_id}: interrupted; end observed at teardown (no flow_end log)"})
    return events


def plot_rtt(directory: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    paths = sorted(directory.glob("*/*.csv"))
    counts = Counter(path.parent for path in paths)
    clients = {
        f"{path.parent.name} ({path.stem})" if counts[path.parent] > 1
        else path.parent.name: read_rtt(path)
        for path in paths
    }
    if not clients:
        raise ValueError(f"{directory}: no relay RTT CSVs")
    clock = "timestamp_mono_ns" if all("timestamp_mono_ns" in rows[0] for rows in clients.values()) else "timestamp_unix_ns"
    origin = min(rows[0][clock] for rows in clients.values())
    events = read_experiment_events(directory)
    events.sort(key=lambda event: event[clock])
    row_count = 4 if events else 3
    figure, axes = plt.subplots(
        row_count, 1, figsize=(16 if events else 13, 9 + 0.28 * len(events)),
        gridspec_kw={"height_ratios": [3, 3, 1.5] + ([max(1.5, 0.28 * len(events))] if events else [])},
        layout="constrained",
    )
    axes[1].sharex(axes[0])
    axes[2].sharex(axes[0])
    for client, rows in clients.items():
        # Align clients by their first wall-clock timestamp, then use monotonic deltas.
        offset = rows[0][clock] - origin
        elapsed_origin = rows[0]["elapsed_ns"]
        seconds = [(offset + row["elapsed_ns"] - elapsed_origin) / 1e9 for row in rows]
        axes[0].plot(seconds, [row["latest_rtt_ms"] for row in rows], label=client, linewidth=0.8)
        axes[1].plot(seconds, [row["srtt_ms"] for row in rows], label=client, linewidth=0.8)
        axes[2].plot(seconds, [row["interval_ns"] / 1e6 for row in rows], label=client, linewidth=0.6)
    axes[2].axhline(1, color="black", linestyle="--", linewidth=0.7, label="1 ms target")
    for axis, label in zip(axes[:3], ("Latest RTT (ms)", "Smoothed RTT (ms)", "Actual sample interval (ms)")):
        axis.set_ylabel(label)
        axis.grid(alpha=0.25)
        axis.legend()
    axes[2].set_xlabel("Seconds since first recorded sample")
    if events:
        table = []
        for index, event in enumerate(events, 1):
            seconds = (event[clock] - origin) / 1e9
            table.append([f"E{index}", f"{seconds:.6f}", event["device"], event["label"]])
            for axis in axes[:3]:
                axis.axvline(seconds, color=event["color"], linestyle="--", linewidth=0.8, alpha=0.7)
            axes[0].text(seconds, 0.98 - 0.06 * ((index - 1) % 4), f"E{index}",
                         transform=axes[0].get_xaxis_transform(), color=event["color"], fontsize=8,
                         va="top", bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.7})
            if "start" in event:
                beginning = (event["start"][clock] - origin) / 1e9
                for axis in axes[:2]:
                    axis.axvspan(beginning, seconds, color="tab:green", alpha=0.05)
        axes[3].axis("off")
        event_table = axes[3].table(cellText=table, colLabels=["Event", "Time (s)", "Device / egress / path", "Change"],
                                    colWidths=[0.04, 0.08, 0.38, 0.50], cellLoc="left", loc="center")
        event_table.auto_set_font_size(False)
        event_table.set_fontsize(8)
        event_table.scale(1, 1.4)
    figure.suptitle("Relay-side QUIC RTT — 1 ms target sampling\nValues update on QUIC feedback; repeated samples are not new RTT measurements")
    output = directory / "rtt.png"
    try:
        figure.savefig(output, dpi=150)
    finally:
        plt.close(figure)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Plot relay QUIC RTT CSVs from a moqlab run.")
    parser.add_argument("directory", type=Path, help="Run's rtt directory")
    args = parser.parse_args()
    plot_rtt(args.directory)
