from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from moqlab import cli as cli_module
from moqlab.cli import cli
from moqlab.doctor import CheckResult
from moqlab.exceptions import OrchestratorError


def test_root_help_mentions_default_backend_and_builds():
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "`run` defaults to the Containernet" in result.output
    assert "build" in result.output
    assert "doctor" in result.output


def test_run_help_points_to_doctor():
    result = CliRunner().invoke(cli, ["run", "--help"])

    assert result.exit_code == 0
    assert "Defaults to containernet" in result.output
    assert "moqlab" in result.output
    assert "doctor -c CONFIG" in result.output
    assert "--vis, --visualize" in result.output


def test_visualize_is_not_a_standalone_command():
    result = CliRunner().invoke(cli, ["visualize", "--help"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_doctor_prints_checks_and_exits_nonzero_on_failure(monkeypatch):
    monkeypatch.setattr(
        cli_module,
        "doctor_checks",
        lambda config_path, backend: [
            CheckResult(
                "docker images",
                "fail",
                "missing moqlab-relay",
                "Run `python -m moqlab build images`.",
                "Build",
            )
        ],
    )

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "moqlab doctor: not ready" in result.output
    assert "Build" in result.output
    assert "FAIL docker images: missing moqlab-relay" in result.output
    assert "build images" in result.output


def test_run_readiness_failure_stops_before_backend(monkeypatch, tmp_path: Path):
    config = tmp_path / "topology.yaml"
    config.write_text("topology_mode: explicit\nrelays:\n  relay-a: { listen_port: 9668, admin_port: 9669 }\n")
    called = False

    def fake_ensure_run_ready(config_path, backend):
        raise OrchestratorError("run readiness failed")

    def fake_up_docker(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cli_module, "ensure_run_ready", fake_ensure_run_ready)
    monkeypatch.setattr(cli_module, "_up_docker", fake_up_docker)

    result = CliRunner().invoke(
        cli,
        ["run", "-c", str(config), "--backend", "docker"],
    )

    assert result.exit_code == 1
    assert "run readiness failed" in result.output
    assert not called


def test_run_visualize_starts_server_and_passes_it_to_backend(monkeypatch, tmp_path: Path):
    config = tmp_path / "topology.yaml"
    config.write_text("topology_mode: explicit\nrelays:\n  relay-a: { listen_port: 9668, admin_port: 9669 }\n")
    server = object()
    seen = {}

    def fake_up_docker(ctx, config, run_id, publish_ports, readiness_timeout, visualizer):
        seen["visualizer"] = visualizer

    monkeypatch.setattr(cli_module, "ensure_run_ready", lambda config_path, backend: None)
    monkeypatch.setattr(cli_module, "_start_visualizer", lambda config_path: server)
    monkeypatch.setattr(cli_module, "_up_docker", fake_up_docker)

    result = CliRunner().invoke(
        cli,
        ["run", "-c", str(config), "--backend", "docker", "--visualize"],
    )

    assert result.exit_code == 0
    assert seen["visualizer"] is server


def test_containernet_error_stops_visualizer_once(monkeypatch):
    stops = []
    server = object()

    def fake_up(self, config_path, run_id):
        raise OrchestratorError("containernet failed")

    monkeypatch.setattr(
        "moqlab.orchestrator.containernet_backend.ContainernetBackend.up",
        fake_up,
    )
    monkeypatch.setattr(cli_module, "_stop_visualizer", lambda visualizer: stops.append(visualizer))

    with pytest.raises(cli_module.click.ClickException):
        cli_module._up_containernet(
            SimpleNamespace(obj={}),
            Path("topology.yaml"),
            None,
            server,
        )

    assert stops == [server]
