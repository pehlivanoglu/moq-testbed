#!/usr/bin/env python3
"""Sample router qdisc counters and instantaneous queue state into CSV."""

from __future__ import annotations

import argparse
import csv
import json
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CSV_FIELDS = (
    "timestamp_mono_ns",
    "interval_ns",
    "collection_duration_ns",
    "offered_bytes",
    "transmitted_bytes",
    "drops",
    "dualpi2_drops",
    "l_enqueued_packets",
    "c_enqueued_packets",
    "ecn_marks",
    "step_marks",
    "queue_capacity_packets",
    "queue_packets",
    "queue_percent",
    "queue_backlog_bytes",
    "queue_memory_capacity_bytes",
    "queue_memory_bytes",
    "queue_memory_percent",
    "l_head_delay_us",
    "c_head_delay_us",
    "separate_queue_packets",
    "separate_queue_backlog_bytes",
    "counter_reset",
)


@dataclass(frozen=True)
class Sample:
    timestamp_mono_ns: int
    collection_duration_ns: int
    offered_bytes: int
    root_transmitted_bytes: int
    root_drops: int
    dualpi2_drops: int
    l_enqueued_packets: int
    c_enqueued_packets: int
    ecn_marks: int
    step_marks: int
    queue_capacity_packets: int
    queue_packets: int
    queue_backlog_bytes: int
    queue_memory_capacity_bytes: int
    queue_memory_bytes: int
    l_head_delay_us: int
    c_head_delay_us: int
    separate_queue_packets: int
    separate_queue_backlog_bytes: int


def _tc_json(*args: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        ["tc", "-j", "-s", "-d", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, list):
        raise RuntimeError(f"tc returned unexpected JSON for {' '.join(args)}")
    return value


def _offered_bytes(filters: list[dict[str, Any]]) -> int:
    for entry in filters:
        if entry.get("kind") != "matchall":
            continue
        for action in entry.get("options", {}).get("actions", []):
            if action.get("kind") == "gact":
                return int(action.get("stats", {}).get("bytes", 0))
    raise RuntimeError("offered-byte matchall counter is missing")


def parse_sample(
    qdiscs: list[dict[str, Any]],
    filters: list[dict[str, Any]],
    timestamp_mono_ns: int,
    collection_duration_ns: int,
) -> Sample:
    try:
        root = next(qdisc for qdisc in qdiscs if qdisc.get("root"))
    except StopIteration as error:
        raise RuntimeError("qdisc hierarchy has no root qdisc") from error

    dualpi2 = next(
        (qdisc for qdisc in qdiscs if qdisc.get("kind") == "dualpi2"), None
    )
    fifo = next((qdisc for qdisc in qdiscs if qdisc.get("kind") == "pfifo"), None)
    queue = dualpi2 or fifo or root

    options = queue.get("options", {})
    capacity = int(options.get("limit", 0))

    # HTB mirrors descendant backlog, so do not sum it with the leaf. Netem
    # is the only separate queue synthesized by moqlab's current hierarchy.
    separate = [q for q in qdiscs if q.get("kind") == "netem"]
    return Sample(
        timestamp_mono_ns=timestamp_mono_ns,
        collection_duration_ns=collection_duration_ns,
        offered_bytes=_offered_bytes(filters),
        root_transmitted_bytes=int(root.get("bytes", 0)),
        root_drops=int(root.get("drops", 0)),
        dualpi2_drops=int(dualpi2.get("drops", 0)) if dualpi2 else 0,
        l_enqueued_packets=int(dualpi2.get("pkts-in-l", 0)) if dualpi2 else 0,
        c_enqueued_packets=int(dualpi2.get("pkts-in-c", 0)) if dualpi2 else 0,
        ecn_marks=int(dualpi2.get("ecn-mark", 0)) if dualpi2 else 0,
        step_marks=int(dualpi2.get("step-mark", 0)) if dualpi2 else 0,
        queue_capacity_packets=capacity,
        queue_packets=int(queue.get("qlen", 0)),
        queue_backlog_bytes=int(queue.get("backlog", 0)),
        queue_memory_capacity_bytes=int(dualpi2.get("memory-limit", 0)) if dualpi2 else 0,
        queue_memory_bytes=int(dualpi2.get("memory-used", 0)) if dualpi2 else 0,
        l_head_delay_us=int(dualpi2.get("delay-l", 0)) if dualpi2 else 0,
        c_head_delay_us=int(dualpi2.get("delay-c", 0)) if dualpi2 else 0,
        separate_queue_packets=sum(int(q.get("qlen", 0)) for q in separate),
        separate_queue_backlog_bytes=sum(int(q.get("backlog", 0)) for q in separate),
    )


def collect(interface: str) -> tuple[Sample, list[dict[str, Any]]]:
    start_ns = time.monotonic_ns()
    filters = _tc_json("filter", "show", "dev", interface, "egress")
    qdiscs = _tc_json("qdisc", "show", "dev", interface)
    end_ns = time.monotonic_ns()
    return parse_sample(qdiscs, filters, end_ns, end_ns - start_ns), qdiscs


def _delta(current: int, previous: int) -> tuple[int, bool]:
    if current < previous:
        return 0, True
    return current - previous, False


def interval_row(previous: Sample, current: Sample) -> dict[str, int | float]:
    deltas: dict[str, int] = {}
    reset = False
    for output, field in (
        ("offered_bytes", "offered_bytes"),
        ("transmitted_bytes", "root_transmitted_bytes"),
        ("drops", "root_drops"),
        ("dualpi2_drops", "dualpi2_drops"),
        ("l_enqueued_packets", "l_enqueued_packets"),
        ("c_enqueued_packets", "c_enqueued_packets"),
        ("ecn_marks", "ecn_marks"),
        ("step_marks", "step_marks"),
    ):
        deltas[output], wrapped = _delta(getattr(current, field), getattr(previous, field))
        reset |= wrapped

    queue_percent = (
        100.0 * current.queue_packets / current.queue_capacity_packets
        if current.queue_capacity_packets
        else float("nan")
    )
    memory_percent = (
        100.0 * current.queue_memory_bytes / current.queue_memory_capacity_bytes
        if current.queue_memory_capacity_bytes
        else 0.0
    )
    return {
        "timestamp_mono_ns": current.timestamp_mono_ns,
        "interval_ns": current.timestamp_mono_ns - previous.timestamp_mono_ns,
        "collection_duration_ns": current.collection_duration_ns,
        **deltas,
        "queue_capacity_packets": current.queue_capacity_packets,
        "queue_packets": current.queue_packets,
        "queue_percent": round(queue_percent, 6),
        "queue_backlog_bytes": current.queue_backlog_bytes,
        "queue_memory_capacity_bytes": current.queue_memory_capacity_bytes,
        "queue_memory_bytes": current.queue_memory_bytes,
        "queue_memory_percent": round(memory_percent, 6),
        "l_head_delay_us": current.l_head_delay_us,
        "c_head_delay_us": current.c_head_delay_us,
        "separate_queue_packets": current.separate_queue_packets,
        "separate_queue_backlog_bytes": current.separate_queue_backlog_bytes,
        "counter_reset": int(reset),
    }


def write_hierarchy(interface: str, path: Path, qdiscs: list[dict[str, Any]]) -> None:
    classes = _tc_json("class", "show", "dev", interface)
    separate = sorted({q["kind"] for q in qdiscs if q.get("kind") == "netem"})
    document = {
        "interface": interface,
        "captured_mono_ns": time.monotonic_ns(),
        "qdiscs": qdiscs,
        "classes": classes,
        "rate_shaper_qdiscs": sorted(
            {q["kind"] for q in qdiscs if q.get("kind") in {"htb", "tbf"}}
        ),
        "separate_queue_qdiscs": separate,
        "limitations": {
            "occupancy": "tc reports occupancy only at each sampling instant",
            "dualpi2_per_queue_occupancy": (
                "not exposed by the Linux tc ABI; pkts-in-l/pkts-in-c are "
                "cumulative enqueue counters"
            ),
            "counter_atomicity": "filter and qdisc counters are read sequentially",
        },
    }
    path.write_text(json.dumps(document, indent=2) + "\n")


def run(interface: str, interval_ms: int, output: Path, hierarchy: Path) -> None:
    stopped = False

    def stop(_signum, _frame) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    output.parent.mkdir(parents=True, exist_ok=True)
    hierarchy.parent.mkdir(parents=True, exist_ok=True)

    previous, qdiscs = collect(interface)
    write_hierarchy(interface, hierarchy, qdiscs)
    hierarchy_signature = _hierarchy_signature(qdiscs)
    period_ns = interval_ms * 1_000_000
    deadline_ns = previous.timestamp_mono_ns + period_ns

    with output.open("w", newline="", buffering=1) as destination:
        writer = csv.DictWriter(destination, fieldnames=CSV_FIELDS)
        writer.writeheader()
        while not stopped:
            remaining_ns = deadline_ns - time.monotonic_ns()
            if remaining_ns > 0:
                time.sleep(remaining_ns / 1_000_000_000)
            current, qdiscs = collect(interface)
            current_signature = _hierarchy_signature(qdiscs)
            if current_signature != hierarchy_signature:
                write_hierarchy(interface, hierarchy, qdiscs)
                hierarchy_signature = current_signature
            writer.writerow(interval_row(previous, current))
            previous = current
            deadline_ns += period_ns
            if deadline_ns <= current.timestamp_mono_ns:
                deadline_ns = current.timestamp_mono_ns + period_ns


def _hierarchy_signature(qdiscs: list[dict[str, Any]]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (qdisc.get("kind"), qdisc.get("handle"), qdisc.get("parent"))
        for qdisc in qdiscs
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--interval-ms", type=int, default=25)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hierarchy", type=Path, required=True)
    args = parser.parse_args()
    if args.interval_ms <= 0:
        parser.error("--interval-ms must be positive")
    run(args.interface, args.interval_ms, args.output, args.hierarchy)


if __name__ == "__main__":
    main()
