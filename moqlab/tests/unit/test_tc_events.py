from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

import pytest

from moqlab.exceptions import OrchestratorError
from moqlab.tc_events import RateChanges, TcEventMonitors, decode_notifications


def _attr(kind: int, data: bytes) -> bytes:
    size = len(data) + 4
    return struct.pack("=HH", size, kind) + data + bytes((-size) % 4)


def _message(kind="htb", rate=100_000_000, ceiling=None, message_type=40,
             handle=0x50001, options=None, flags=0) -> bytes:
    if options is None:
        def ratespec(bps):
            return struct.pack("=BBHhHI", 0, 0, 0, 0, 0, min(bps // 8, 0xFFFFFFFF))
        parameters = ratespec(rate) + ratespec(ceiling or rate) + bytes(20)
        options = _attr(1, parameters) + _attr(6, struct.pack("=Q", rate // 8))
        options += _attr(7, struct.pack("=Q", (ceiling or rate) // 8))
    payload = struct.pack("=BBHiIII", 0, 0, 0, 7, handle, 0x50000, 0)
    payload += _attr(1, kind.encode() + b"\0") + _attr(2, options)
    return struct.pack("=IHHII", len(payload) + 16, message_type, flags, 1234, 0) + payload


def test_kernel_events_preserve_fast_changes_and_actual_64bit_rates() -> None:
    state = RateChanges()
    baseline = decode_notifications(_message(flags=2))[0]
    assert state.update(baseline, initial=True)[0]["event"] == "bandwidth_initial"
    changes = []
    # Two back-to-back changes in one read: a state poll would collapse these.
    for message in decode_notifications(_message(rate=10_000_000) + _message(rate=80_000_000_000)):
        assert not message["multipart"]  # Multicast events can have nonzero sequence numbers.
        changes.extend(state.update(message))
    assert [event["after"]["rate_bps"] for event in changes] == [10_000_000, 80_000_000_000]
    assert changes[1]["before"]["rate_bps"] == 10_000_000
    assert changes[0]["handle"] == "5:1"
    assert state.update(decode_notifications(_message(rate=80_000_000_000))[0]) == []


def test_ceiling_changes_and_parent_deletion_are_recorded() -> None:
    state = RateChanges()
    state.update(decode_notifications(_message())[0], initial=True)
    event = state.update(decode_notifications(_message(ceiling=120_000_000))[0])[0]
    assert event["before"]["rate_bps"] == event["after"]["rate_bps"]
    assert event["after"]["ceil_bps"] == 120_000_000
    removed = state.update(decode_notifications(_message(message_type=37, handle=0x50000, options=b""))[0])
    assert len(removed) == 1
    assert removed[0]["object"] == "class"
    assert removed[0]["after"] == {}
    assert state.rates == {}


@pytest.mark.parametrize("kind, options, expected", [
    ("tbf", _attr(1, bytes(8) + struct.pack("=I", 1_250_000) + bytes(24))
     + _attr(4, struct.pack("=Q", 10_000_000_000)), 80_000_000_000),
    ("netem", bytes(24) + _attr(6, struct.pack("=Iiii", 1_250_000, 0, 0, 0)), 10_000_000),
    ("cake", _attr(2, struct.pack("=Q", 1_250_000)), 10_000_000),
])
def test_qdisc_rate_decoding(kind, options, expected) -> None:
    event = decode_notifications(_message(kind=kind, message_type=36, options=options))[0]
    assert event["rates"]["rate_bps"] == expected


def test_aqm_options_are_not_interpreted_as_rates_and_bad_messages_fail() -> None:
    event = decode_notifications(_message(kind="fq_codel", options=bytes(13)))[0]
    assert event["rates"] == {}
    assert RateChanges().update(event) == []
    with pytest.raises(ValueError, match="truncated"):
        decode_notifications(_message()[:-1])
    with pytest.raises(RuntimeError, match="overrun"):
        decode_notifications(struct.pack("=IHHII", 16, 4, 0, 0, 0))


def test_namespace_monitor_lifecycle_covers_every_node(tmp_path: Path, monkeypatch) -> None:
    commands = []

    class Process:
        status = None

        def __init__(self, argv, **_kwargs):
            commands.append(argv)
            Path(argv[argv.index("--ready") + 1]).touch()

        def poll(self):
            return self.status

        def terminate(self):
            self.status = 0

        def wait(self, timeout=None):
            return self.status

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "101\n202\n"))
    monkeypatch.setattr(subprocess, "Popen", Process)
    monitors = TcEventMonitors(tmp_path, ["west", "east"])
    monitors.start()
    assert [argv[:4] for argv in commands] == [["nsenter", "-t", "101", "-n"], ["nsenter", "-t", "202", "-n"]]
    monitors.stop()
    assert monitors.processes == []


@pytest.mark.parametrize("status", [0, 2])
def test_monitor_failure_is_visible_in_archive(tmp_path: Path, status: int) -> None:
    class Failed:
        def poll(self):
            return status

        def wait(self, timeout=None):
            return status

    monitors = TcEventMonitors(tmp_path, [])
    monitors.processes = [("east", Failed())]
    with pytest.raises(OrchestratorError, match="incomplete for east"):
        monitors.stop()
    event = json.loads((tmp_path / "east.jsonl").read_text())
    assert event["event"] == "monitor_error"
