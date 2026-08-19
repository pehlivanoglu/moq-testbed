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


def test_doctor_uses_custom_image_names_from_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config = tmp_path / "topology.yaml"
    config.write_text(
        "\n".join(
            [
                "topology_mode: explicit",
                "relays:",
                "  relay-a: { listen_port: 9668, admin_port: 9669, image: custom-relay }",
                "publishers:",
                "  pub: { connects_to: relay-a, image: custom-pub }",
                "subscribers:",
                "  sub: { connects_to: relay-a, namespace: msf/clear, track: video/s2, image: custom-sub }",
            ]
        )
    )
    seen_tags: set[str] = set()

    def fake_images_check(image_tags: set[str]) -> CheckResult:
        seen_tags.update(image_tags)
        return CheckResult("docker images", "ok", "mocked", category="Build")

    monkeypatch.setattr(doctor, "_docker_daemon_check", lambda: CheckResult("docker daemon", "ok", "mocked"))
    monkeypatch.setattr(doctor, "_docker_images_check", fake_images_check)

    doctor_checks(config_path=config, backend="docker", include_build_artifacts=False)

    assert seen_tags == {"custom-relay", "custom-pub", "custom-sub"}


def test_doctor_docker_backend_skips_containernet_checks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(doctor, "_docker_daemon_check", lambda: CheckResult("docker daemon", "ok", "mocked"))
    monkeypatch.setattr(doctor, "_docker_images_check", lambda image_tags: CheckResult("docker images", "ok", "mocked"))

    checks = doctor_checks(backend="docker", include_build_artifacts=False)

    assert "containernet python" not in {check.name for check in checks}
    assert "containernet privileges" not in {check.name for check in checks}
    assert "kernel dualpi2" not in {check.name for check in checks}


def test_doctor_checks_media_toolchain_for_media_topology(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    config = tmp_path / "media.yaml"
    config.write_text(
        """\
defaults:
  relay:
    tls: {insecure: false, generated: true}
relays:
  relay-a: {listen_port: 9668, admin_port: 9669}
publishers:
  pub:
    kind: media
    connects_to: relay-a
    asset: testsvc
    listen_port: 4443
    fingerprint_port: 8081
subscribers:
  sub:
    kind: media
    connects_to: relay-a
    namespace: msf/clear
    track: video/s2
"""
    )
    monkeypatch.setattr(
        doctor, "_docker_daemon_check", lambda: CheckResult("docker daemon", "ok", "mocked")
    )
    monkeypatch.setattr(
        doctor,
        "_docker_images_check",
        lambda image_tags: CheckResult("docker images", "ok", "mocked"),
    )
    monkeypatch.setattr(
        doctor, "_openssl_check", lambda: CheckResult("openssl", "ok", "mocked")
    )
    monkeypatch.setattr(
        doctor, "_buildkit_check", lambda: CheckResult("buildkit", "ok", "mocked")
    )
    monkeypatch.setattr(
        doctor,
        "_media_context_checks",
        lambda root: [CheckResult("media contexts", "ok", str(root))],
    )

    checks = doctor_checks(
        root=tmp_path,
        config_path=config,
        backend="docker",
        include_build_artifacts=False,
    )

    assert {"openssl", "buildkit", "media contexts"} <= {
        check.name for check in checks
    }


def test_doctor_containernet_backend_includes_dualpi2_check(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(doctor, "_docker_daemon_check", lambda: CheckResult("docker daemon", "ok", "mocked"))
    monkeypatch.setattr(doctor, "_docker_images_check", lambda image_tags: CheckResult("docker images", "ok", "mocked"))

    checks = doctor_checks(backend="containernet", include_build_artifacts=False)

    dualpi2 = next(check for check in checks if check.name == "kernel dualpi2")
    assert dualpi2.status in {"ok", "warn"}  # warn-only check, never blocks


def test_ensure_run_ready_does_not_require_build_artifacts(monkeypatch: pytest.MonkeyPatch):
    kwargs_seen = {}

    def fake_checks(**kwargs):
        kwargs_seen.update(kwargs)
        return [CheckResult("docker images", "ok", "mocked")]

    monkeypatch.setattr(doctor, "doctor_checks", fake_checks)

    ensure_run_ready("topology.yaml", "docker")

    assert kwargs_seen["include_build_artifacts"] is False


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


def test_ensure_run_ready_rejects_non_root_containernet(monkeypatch: pytest.MonkeyPatch):
    def fake_checks(**kwargs):
        return [
            CheckResult(
                "containernet privileges",
                "warn",
                "current process is not root",
                "Run containernet topologies with `sudo <venv>/bin/python -m moqlab run ...`.",
            )
        ]

    monkeypatch.setattr(doctor, "doctor_checks", fake_checks)

    with pytest.raises(OrchestratorError) as excinfo:
        ensure_run_ready("topology.yaml", "containernet")

    message = str(excinfo.value)
    assert "containernet privileges" in message
    assert "current process is not root" in message
