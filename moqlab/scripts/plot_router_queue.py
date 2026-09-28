#!/usr/bin/env python3
"""Plot a router queue-metrics CSV produced by moqlab."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def read_rows(path: Path) -> list[dict[str, float]]:
    with path.open(newline="") as source:
        rows = [
            {key: float(value) for key, value in row.items()}
            for row in csv.DictReader(source)
        ]
    if not rows:
        raise ValueError(f"{path}: no samples")
    return rows


def plot(path: Path, output: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = read_rows(path)
    origin = rows[0]["timestamp_mono_ns"]
    seconds = [(row["timestamp_mono_ns"] - origin) / 1e9 for row in rows]
    ingress_mbps = [row["offered_bytes"] * 8e3 / row["interval_ns"] for row in rows]
    egress_mbps = [row["transmitted_bytes"] * 8e3 / row["interval_ns"] for row in rows]

    figure, axes = plt.subplots(4, 1, figsize=(13, 11), sharex=True, layout="constrained")
    axes[0].plot(seconds, ingress_mbps, label="offered before qdisc drops")
    axes[0].plot(seconds, egress_mbps, label="transmitted")
    axes[0].set_ylabel("Mbit/s")
    axes[0].legend()

    axes[1].plot(seconds, [row["queue_percent"] for row in rows], label="selected queue")
    axes[1].plot(
        seconds,
        [row["separate_queue_packets"] for row in rows],
        label="separate qdisc (packets)",
    )
    axes[1].set_ylabel("Occupancy % / packets")
    axes[1].legend()

    axes[2].step(seconds, [row["drops"] for row in rows], where="post", label="all qdiscs")
    axes[2].step(
        seconds,
        [row["dualpi2_drops"] for row in rows],
        where="post",
        label="DualPI2 only",
    )
    axes[2].set_ylabel("Drops / interval")
    axes[2].legend()

    axes[3].plot(seconds, [row["l_head_delay_us"] / 1000 for row in rows], label="L head")
    axes[3].plot(seconds, [row["c_head_delay_us"] / 1000 for row in rows], label="C head")
    axes[3].set_ylabel("Head delay (ms)")
    axes[3].set_xlabel("Seconds since first sample")
    axes[3].legend()

    for axis in axes:
        axis.grid(alpha=0.25)
    figure.suptitle(
        f"Router queue telemetry — {path.name}\n"
        "occupancy is instantaneous at each poll, not an interval minimum/maximum"
    )
    figure.savefig(output, dpi=150)
    plt.close(figure)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    plot(args.csv, args.output or args.csv.with_suffix(".png"))
