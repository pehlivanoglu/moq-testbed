from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from moqlab.exceptions import OrchestratorError


@dataclass(frozen=True)
class BuildCommand:
    label: str
    argv: list[str]
    cwd: Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def moqx_build_commands(root: Path | None = None) -> list[BuildCommand]:
    root = root or repo_root()
    script = root / "scripts" / "build.sh"
    if not script.exists():
        raise OrchestratorError(f"moqx build script not found: {script}")

    commands: list[BuildCommand] = []
    if not _moxygen_binaries_ready(root):
        commands.append(
            BuildCommand(
                label="setup moxygen dependencies",
                argv=[str(script), "setup"],
                cwd=root,
            )
        )
    commands.append(
        BuildCommand(
            label="build moqx",
            argv=[str(script)],
            cwd=root,
        )
    )
    return commands


def docker_image_build_commands(root: Path | None = None) -> list[BuildCommand]:
    root = root or repo_root()
    _ensure_image_artifacts(root)

    specs = [
        ("relay image", "moqlab/docker/Dockerfile.relay", "moqlab-relay"),
        ("publisher image", "moqlab/docker/Dockerfile.pub", "moqlab-pub"),
        ("subscriber image", "moqlab/docker/Dockerfile.sub", "moqlab-sub"),
        ("router image", "moqlab/docker/Dockerfile.router", "moqlab-router"),
        ("traffic image", "moqlab/docker/Dockerfile.traffic", "moqlab-traffic"),
    ]
    return [
        BuildCommand(
            label=f"build {label}",
            argv=["docker", "build", "-f", dockerfile, "-t", tag, "."],
            cwd=root,
        )
        for label, dockerfile, tag in specs
    ]


def media_image_build_commands(
    root: Path | None = None,
    publisher_context: Path | None = None,
    player_context: Path | None = None,
) -> list[BuildCommand]:
    """Plan media images using dirty local sibling repositories as contexts."""
    root = root or repo_root()
    publisher_context = publisher_context or root.parent / "moqlivemock-svc"
    player_context = player_context or root.parent / "warp-player-svc"
    for label, context, marker in (
        ("publisher", publisher_context, "go.mod"),
        ("player", player_context, "package.json"),
    ):
        if not context.is_dir() or not (context / marker).is_file():
            raise OrchestratorError(
                f"media {label} source context is invalid: {context} (missing {marker})"
            )
    return [
        BuildCommand(
            label="build media publisher image",
            argv=[
                "docker",
                "build",
                "--build-context",
                f"mlmpub={publisher_context.resolve()}",
                "-f",
                "moqlab/docker/Dockerfile.media-pub",
                "-t",
                "moqlab-media-pub",
                ".",
            ],
            cwd=root,
        ),
        BuildCommand(
            label="build media subscriber image",
            argv=[
                "docker",
                "build",
                "--build-context",
                f"player={player_context.resolve()}",
                "-f",
                "moqlab/docker/Dockerfile.media-sub",
                "-t",
                "moqlab-media-sub",
                ".",
            ],
            cwd=root,
        ),
        BuildCommand(
            label="build native media subscriber image",
            argv=[
                "docker",
                "build",
                "--build-context",
                f"mlmpub={publisher_context.resolve()}",
                "-f",
                "moqlab/docker/Dockerfile.media-native-sub",
                "-t",
                "moqlab-media-native-sub",
                ".",
            ],
            cwd=root,
        ),
    ]


def run_build_command(command: BuildCommand) -> None:
    try:
        subprocess.run(command.argv, cwd=command.cwd, check=True)
    except FileNotFoundError as e:
        raise OrchestratorError(
            f"build command executable not found: {command.argv[0]}"
        ) from e
    except subprocess.CalledProcessError as e:
        raise OrchestratorError(
            f"{command.label} failed with exit code {e.returncode}"
        ) from e


def _moxygen_binaries_ready(root: Path) -> bool:
    return all(path.exists() for path in _moxygen_binary_paths(root))


def missing_image_artifacts(root: Path | None = None) -> list[Path]:
    root = root or repo_root()
    required = [root / "build" / "moqx", *_moxygen_binary_paths(root)]
    return [path for path in required if not path.exists()]


def _ensure_image_artifacts(root: Path) -> None:
    missing = missing_image_artifacts(root)
    if missing:
        formatted = "\n".join(f"  - {path}" for path in missing)
        raise OrchestratorError(
            "Docker image build artifacts are missing. Run "
            "`python -m moqlab build moqx` first.\n"
            f"Missing:\n{formatted}"
        )


def _moxygen_binary_paths(root: Path) -> list[Path]:
    bin_dir = root / ".scratch" / "moxygen-install" / "bin"
    return [
        bin_dir / "moqdateserver",
        bin_dir / "moqtextclient",
    ]
