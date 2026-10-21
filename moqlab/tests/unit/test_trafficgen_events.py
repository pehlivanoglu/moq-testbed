from __future__ import annotations

import asyncio

import pytest

from moqlab import trafficgen


@pytest.mark.parametrize("outcome", ["completed", "interrupted", "error"])
def test_flow_records_actual_start_and_end_status(monkeypatch, outcome) -> None:
    records = []
    monkeypatch.setattr(trafficgen, "_log", lambda event, **fields: records.append({"event": event, **fields}))

    async def runner(*args):
        if outcome == "interrupted":
            raise asyncio.CancelledError
        if outcome == "error":
            raise ValueError("sender failed")

    monkeypatch.setattr(trafficgen, "_run_cbr", runner)

    async def exercise():
        flow = {"id": "load", "kind": "cbr", "route": "shared", "duration_s": 30, "start_s": 0}
        try:
            await trafficgen._run_flow({}, flow, 0, 0)
        except (asyncio.CancelledError, ValueError):
            assert outcome != "completed"

    asyncio.run(exercise())
    assert [event["event"] for event in records] == ["flow_start", "flow_end"]
    assert records[-1]["status"] == outcome


def test_cancelled_future_flow_is_not_marked_as_started(monkeypatch) -> None:
    records = []
    monkeypatch.setattr(trafficgen, "_log", lambda event, **fields: records.append(event))

    async def exercise():
        flow = {"id": "future", "kind": "cbr", "route": "shared", "duration_s": 30, "start_s": 1000}
        task = asyncio.create_task(trafficgen._run_flow({}, flow, 0, asyncio.get_running_loop().time()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert records == []
