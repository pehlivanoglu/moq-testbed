from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("MOQLAB_INTEGRATION") != "1",
        reason="set MOQLAB_INTEGRATION=1 to run Docker media acceptance",
    ),
]


def test_headless_svc_playback_and_cleanup(tmp_path: Path):
    from moqlab.exceptions import RunNotFoundError
    from moqlab.orchestrator.docker_backend import DockerBackend

    config = (
        Path(__file__).resolve().parents[2]
        / "configs/examples/media_svc_headless.yaml"
    )
    backend = DockerBackend(runs_dir=tmp_path / "runs")
    run_id = "integration-media-svc"
    try:
        record = backend.up(config, run_id=run_id, readiness_timeout_s=15)
        subscriber = backend.container_for(record.run_id, "sub")
        result = subscriber.exec_run(["cat", "/tmp/moqlab-media-ready.json"])
        readiness = json.loads(result.output)
        assert readiness["status"] == "ready"
        assert (readiness["width"], readiness["height"]) == (1280, 720)
        logs = subscriber.logs().decode("utf-8", errors="replace")
        assert '\"expectedSubscriptions\":3' in logs
        assert logs.count('\"event\":\"subscription\"') == 3
        assert (
            subscriber.exec_run(["test", "-s", "/tmp/moqlab-first-frame.png"])
            .exit_code
            == 0
        )
    finally:
        try:
            backend.down(run_id)
        except RunNotFoundError:
            pass
    assert all(item.run_id != run_id for item in backend.ls())
