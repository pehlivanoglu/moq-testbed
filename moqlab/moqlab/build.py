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
    ]
    return [
        BuildCommand(
            label=f"build {label}",
            argv=["docker", "build", "-f", dockerfile, "-t", tag, "."],
            cwd=root,
        )
        for label, dockerfile, tag in specs
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
