from __future__ import annotations

from dataclasses import replace

from moqlab.router_metrics import interval_row, parse_sample


def _qdiscs() -> list[dict]:
    return [
        {
            "kind": "htb",
            "handle": "5:",
            "root": True,
            "bytes": 1000,
            "drops": 3,
            "backlog": 200,
            "qlen": 2,
        },
        {
            "kind": "netem",
            "handle": "10:",
            "parent": "5:1",
            "bytes": 1000,
            "drops": 3,
            "backlog": 200,
            "qlen": 2,
        },
        {
            "kind": "dualpi2",
            "handle": "20:",
            "parent": "10:1",
            "options": {"limit": 200},
            "bytes": 1000,
            "drops": 1,
            "backlog": 120,
            "qlen": 1,
            "pkts-in-l": 20,
            "pkts-in-c": 30,
            "ecn-mark": 4,
            "step-mark": 5,
            "delay-l": 100,
            "delay-c": 200,
            "memory-used": 400,
            "memory-limit": 1000,
        },
    ]


def _filters() -> list[dict]:
    return [
        {
            "kind": "matchall",
            "options": {
                "actions": [
                    {"kind": "gact", "stats": {"bytes": 1300, "packets": 12}}
                ]
            },
        }
    ]


def test_parse_sample_uses_root_totals_leaf_occupancy_and_separate_netem():
    sample = parse_sample(_qdiscs(), _filters(), 10_000, 500)

    assert sample.offered_bytes == 1300
    assert sample.root_transmitted_bytes == 1000
    assert sample.root_drops == 3
    assert sample.dualpi2_drops == 1
    assert sample.queue_capacity_packets == 200
    assert sample.queue_packets == 1
    assert sample.separate_queue_packets == 2
    assert sample.l_enqueued_packets == 20
    assert sample.c_enqueued_packets == 30


def test_parse_sample_uses_pfifo_when_dualpi2_is_absent():
    qdiscs = [
        {"kind": "htb", "root": True, "bytes": 1000, "drops": 3},
        {
            "kind": "pfifo",
            "parent": "5:1",
            "options": {"limit": 200},
            "backlog": 1200,
            "qlen": 10,
        },
    ]

    sample = parse_sample(qdiscs, _filters(), 10_000, 500)

    assert sample.queue_capacity_packets == 200
    assert sample.queue_packets == 10
    assert sample.queue_backlog_bytes == 1200
    assert sample.dualpi2_drops == 0
    assert sample.l_enqueued_packets == 0
    assert sample.c_enqueued_packets == 0


def test_interval_row_differences_counters_but_keeps_instantaneous_state():
    previous = parse_sample(_qdiscs(), _filters(), 10_000, 500)
    current = replace(
        previous,
        timestamp_mono_ns=25_010_000,
        collection_duration_ns=700,
        offered_bytes=2300,
        root_transmitted_bytes=1800,
        root_drops=5,
        dualpi2_drops=2,
        l_enqueued_packets=24,
        c_enqueued_packets=36,
        ecn_marks=7,
        step_marks=9,
        queue_packets=50,
        queue_memory_bytes=500,
    )

    row = interval_row(previous, current)

    assert row["interval_ns"] == 25_000_000
    assert row["offered_bytes"] == 1000
    assert row["transmitted_bytes"] == 800
    assert row["drops"] == 2
    assert row["dualpi2_drops"] == 1
    assert row["l_enqueued_packets"] == 4
    assert row["c_enqueued_packets"] == 6
    assert row["queue_packets"] == 50
    assert row["queue_percent"] == 25.0
    assert row["queue_memory_percent"] == 50.0
    assert row["counter_reset"] == 0


def test_interval_row_marks_counter_reset_without_negative_delta():
    previous = parse_sample(_qdiscs(), _filters(), 10_000, 500)
    current = replace(previous, timestamp_mono_ns=20_000, offered_bytes=10)

    row = interval_row(previous, current)

    assert row["offered_bytes"] == 0
    assert row["counter_reset"] == 1
