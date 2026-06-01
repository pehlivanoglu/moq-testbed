from __future__ import annotations

from pathlib import Path

import pytest

from moqlab import doctor
from moqlab.doctor import CheckResult, doctor_checks, ensure_run_ready
from moqlab.exceptions import OrchestratorError


def test_doctor_reports_missing_build_artifacts(tmp_path: Path):
    checks = doctor_checks(root=tmp_path, backend="docker")

    artifact_check = next(check for check in checks if check.name == "moqx artifacts")

    assert artifact_check.status == "fail"
    assert "build/moqx" in artifact_check.message
    assert "build moqx" in artifact_check.next_step


def test_ensure_run_ready_formats_failures(monkeypatch: pytest.MonkeyPatch):
    def fake_checks(**kwargs):
        return [
            CheckResult(
                "docker images",
                "fail",
                "missing moqlab-relay",
                "Run `python -m moqlab build images`.",
            )
        ]

    monkeypatch.setattr(doctor, "doctor_checks", fake_checks)

    with pytest.raises(OrchestratorError) as excinfo:
        ensure_run_ready("topology.yaml", "docker")

    message = str(excinfo.value)
    assert "run readiness failed" in message
    assert "missing moqlab-relay" in message
    assert "build images" in message
