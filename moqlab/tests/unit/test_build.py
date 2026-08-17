"""Unit tests for moqlab.build command planning."""

from __future__ import annotations

from pathlib import Path

import pytest

from moqlab.build import (
    docker_image_build_commands,
    media_image_build_commands,
    moqx_build_commands,
)
from moqlab.exceptions import OrchestratorError


def _write_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env sh\n")
    path.chmod(path.stat().st_mode | 0o111)


def _write_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")


def _root_with_build_script(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write_executable(root / "scripts" / "build.sh")
    return root


def _add_binary_artifacts(root: Path) -> None:
    _write_file(root / "build" / "moqx")
    _write_file(root / ".scratch" / "moxygen-install" / "bin" / "moqdateserver")
    _write_file(root / ".scratch" / "moxygen-install" / "bin" / "moqtextclient")


def test_moqx_build_runs_setup_when_moxygen_artifacts_are_missing(tmp_path: Path):
    root = _root_with_build_script(tmp_path)

    commands = moqx_build_commands(root)

    assert [command.label for command in commands] == [
        "setup moxygen dependencies",
        "build moqx",
    ]
    assert commands[0].argv == [str(root / "scripts" / "build.sh"), "setup"]
    assert commands[1].argv == [str(root / "scripts" / "build.sh")]
    assert all(command.cwd == root for command in commands)


def test_moqx_build_skips_setup_when_moxygen_artifacts_exist(tmp_path: Path):
    root = _root_with_build_script(tmp_path)
    _add_binary_artifacts(root)

    commands = moqx_build_commands(root)

    assert [command.label for command in commands] == ["build moqx"]
    assert commands[0].argv == [str(root / "scripts" / "build.sh")]


def test_docker_image_build_commands_use_repo_root_context(tmp_path: Path):
    root = _root_with_build_script(tmp_path)
    _add_binary_artifacts(root)

    commands = docker_image_build_commands(root)

    assert [command.argv for command in commands] == [
        [
            "docker",
            "build",
            "-f",
            "moqlab/docker/Dockerfile.relay",
            "-t",
            "moqlab-relay",
            ".",
        ],
        [
            "docker",
            "build",
            "-f",
            "moqlab/docker/Dockerfile.pub",
            "-t",
            "moqlab-pub",
            ".",
        ],
        [
            "docker",
            "build",
            "-f",
            "moqlab/docker/Dockerfile.sub",
            "-t",
            "moqlab-sub",
            ".",
        ],
        [
            "docker",
            "build",
            "-f",
            "moqlab/docker/Dockerfile.router",
            "-t",
            "moqlab-router",
            ".",
        ],
        [
            "docker",
            "build",
            "-f",
            "moqlab/docker/Dockerfile.traffic",
            "-t",
            "moqlab-traffic",
            ".",
        ],
    ]
    assert all(command.cwd == root for command in commands)


def test_docker_image_build_requires_binary_artifacts(tmp_path: Path):
    root = _root_with_build_script(tmp_path)

    with pytest.raises(OrchestratorError, match="build moqx"):
        docker_image_build_commands(root)


def test_media_image_build_uses_named_local_contexts(tmp_path: Path):
    root = tmp_path / "moq-testbed"
    publisher = tmp_path / "publisher"
    player = tmp_path / "player"
    _write_file(publisher / "go.mod")
    _write_file(player / "package.json")

    commands = media_image_build_commands(root, publisher, player)

    assert commands[0].argv[2:4] == ["--build-context", f"mlmpub={publisher.resolve()}"]
    assert "moqlab/docker/Dockerfile.media-pub" in commands[0].argv
    assert commands[1].argv[2:4] == ["--build-context", f"player={player.resolve()}"]
    assert commands[2].argv[2:4] == ["--build-context", f"mlmpub={publisher.resolve()}"]
    assert "moqlab/docker/Dockerfile.media-native-sub" in commands[2].argv


def test_media_image_build_rejects_missing_context(tmp_path: Path):
    with pytest.raises(OrchestratorError, match="publisher source context"):
        media_image_build_commands(tmp_path, tmp_path / "missing", tmp_path / "also-missing")
