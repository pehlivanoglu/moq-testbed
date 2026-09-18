#!/usr/bin/env python3
"""Plot one relay's SBD archive; matplotlib is an optional analysis dependency."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_snapshots(path: Path) -> list[dict]:
    snapshots = []
    with path.open() as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                snapshot = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{number}: incomplete or invalid JSONL record") from error
            if snapshot.get("schema_version") != 1:
                raise ValueError(f"{path}:{number}: unsupported schema")
            snapshots.append(snapshot)
    if not snapshots:
        raise ValueError(f"{path}: no snapshots")
    return snapshots


def plot(path: Path, output: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    snapshots = read_snapshots(path)
    start = snapshots[0]["timestamp_ms"]
    ids = sorted({c["connection_id"] for s in snapshots for c in s["clients"]})
    memberships = sorted({tuple(c["group"]) for s in snapshots for c in s["clients"] if c["group"]})
    group_numbers = {members: i + 1 for i, members in enumerate(memberships)}
    fig, axes = plt.subplots(5, 1, figsize=(14, 14), sharex=True, layout="constrained")
    fields = [("skew_est", "Skew estimate"), ("var_est_us", "PDV2 (µs)" if snapshots[0].get("variability_estimator") == "pdv2" else "Variability (µs)"),
              ("freq_est", "Oscillation estimate"), ("pkt_loss", "Packet loss fraction")]
    colors = plt.get_cmap("tab20")
    for index, cid in enumerate(ids):
        records = [(s, c) for s in snapshots for c in s["clients"] if c["connection_id"] == cid]
        times = [(s["timestamp_ms"] - start) / 1000 for s, _ in records]
        label = f"{cid} ({records[0][1]['peer']})"
        for ax, (field, title) in zip(axes[:4], fields):
            values = [c[field] if c["status"] in ("warming_up", "ready") else float("nan") for _, c in records]
            ax.plot(times, values, label=label, color=colors(index % 20), linewidth=1)
            ax.set_ylabel(title)
            ax.grid(alpha=0.25)
        for x, (_, client) in zip(times, records):
            members = tuple(client["group"])
            if members:
                color = colors((group_numbers[members] - 1) % 20)
                axes[4].scatter(x, index, color=color, marker="s", s=8)
            elif client["status"] == "ready":
                axes[4].scatter(x, index, color="black", marker=".", s=5)
            else:
                axes[4].scatter(x, index, color="lightgray", marker="x", s=5)
    axes[0].legend(fontsize="small", loc="upper left")
    axes[4].set_yticks(range(len(ids)), ids, fontsize="small")
    axes[4].set_ylabel("Group membership")
    axes[4].set_xlabel("Seconds since first archived snapshot")
    legend = [Patch(color=colors((number - 1) % 20), label=f"G{number}: {', '.join(members)}")
              for members, number in group_numbers.items()]
    legend += [Patch(color="black", label="Ready, not grouped"), Patch(color="lightgray", label="Not ready / stopped / closed")]
    axes[4].legend(handles=legend, fontsize="x-small", loc="upper left", bbox_to_anchor=(0, -0.18))
    modes = sorted({s["delay_source"] for s in snapshots})
    fig.suptitle(f"SBD ({snapshots[0].get('algorithm', 'unspecified')}) — {snapshots[0]['relay_id']} — {', '.join(modes)}")
    fig.savefig(output, dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    plot(args.archive, args.output or args.archive.with_suffix(".png"))
