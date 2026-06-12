from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from moqlab.build import missing_image_artifacts, repo_root
from moqlab.config.schema import TopologyConfig, load_topology
from moqlab.exceptions import OrchestratorError
from moqlab.runtime import topology_image_tags

CheckStatus = Literal["ok", "warn", "fail"]

_DEFAULT_IMAGE_TAGS = ("moqlab-relay", "moqlab-pub", "moqlab-sub")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    next_step: str | None = None
    category: str = "Environment"


def doctor_checks(
    config_path: str | Path | None = None,
    backend: str = "containernet",
    root: Path | None = None,
    include_build_artifacts: bool = True,
) -> list[CheckResult]:
    root = root or repo_root()
    topology: TopologyConfig | None = None
    checks = [
        CheckResult("python", "ok", sys.executable),
        _python_dependencies_check(),
        _docker_daemon_check(),
    ]
    if include_build_artifacts:
        checks.append(_image_artifacts_check(root))

    if config_path is not None:
        try:
            topology = load_topology(config_path)
        except Exception as e:
            checks.append(
                CheckResult(
                    "config",
                    "fail",
                    str(e),
                    "Fix the topology YAML, then run `python -m moqlab validate -c <config>`.",
                )
            )
        else:
            checks.append(CheckResult("config", "ok", str(config_path), category="Topology"))

    checks.append(_docker_images_check(_image_tags(topology)))

    if backend.lower() == "containernet":
        checks.append(_containernet_import_check())
        checks.append(_containernet_privilege_check())
    return checks


def ensure_run_ready(config_path: str | Path, backend: str) -> None:
    checks = doctor_checks(
        config_path=config_path,
        backend=backend,
        include_build_artifacts=False,
    )
    failures = [check for check in checks if check.status == "fail"]
    if backend.lower() == "containernet":
        failures.extend(
            check
            for check in checks
            if check.name == "containernet privileges" and check.status == "warn"
        )
    if not failures:
        return

    lines = ["run readiness failed:"]
    for check in failures:
        lines.append(f"- {check.name}: {check.message}")
        if check.next_step:
            lines.append(f"  next: {check.next_step}")
    lines.append("Run `python -m moqlab doctor -c <config>` for the full environment report.")
    raise OrchestratorError("\n".join(lines))


def has_failures(checks: list[CheckResult]) -> bool:
    return any(check.status == "fail" for check in checks)


def _python_dependencies_check() -> CheckResult:
    missing = [
        package
        for package, import_name in (
            ("click", "click"),
            ("pydantic", "pydantic"),
            ("PyYAML", "yaml"),
            ("docker", "docker"),
        )
        if importlib.util.find_spec(import_name) is None
    ]
    if not missing:
        return CheckResult("python deps", "ok", "runtime dependencies import")
    return CheckResult(
        "python deps",
        "fail",
        "missing " + ", ".join(missing),
        "Install dependencies into the Python environment you use to run moqlab.",
    )


def _image_artifacts_check(root: Path) -> CheckResult:
    missing = missing_image_artifacts(root)
    if not missing:
        return CheckResult(
            "moqx artifacts",
            "ok",
            "local binaries are present",
            category="Build",
        )
    formatted = ", ".join(str(path.relative_to(root)) for path in missing)
    return CheckResult(
        "moqx artifacts",
        "fail",
        f"missing {formatted}",
        "Run `python -m moqlab build moqx`.",
        "Build",
    )


def _docker_daemon_check() -> CheckResult:
    try:
        import docker

        client = docker.from_env()
        client.ping()
    except Exception as e:
        return CheckResult(
            "docker daemon",
            "fail",
            _short_error(e),
            "Start Docker and make sure this user can access the daemon.",
        )
    return CheckResult("docker daemon", "ok", "reachable")


def _docker_images_check(image_tags: set[str]) -> CheckResult:
    try:
        import docker
        from docker.errors import ImageNotFound

        client = docker.from_env()
        client.ping()
        missing: list[str] = []
        for tag in sorted(image_tags):
            try:
                client.images.get(tag)
            except ImageNotFound:
                missing.append(tag)
    except Exception as e:
        return CheckResult(
            "docker images",
            "fail",
            f"cannot inspect local images: {_short_error(e)}",
            "Fix Docker daemon access first.",
            "Build",
        )

    if not missing:
        return CheckResult(
            "docker images",
            "ok",
            "required images are present",
            category="Build",
        )
    return CheckResult(
        "docker images",
        "fail",
        "missing " + ", ".join(missing),
        "Run `python -m moqlab build images`.",
        "Build",
    )


def _containernet_import_check() -> CheckResult:
    try:
        import mininet.net  # noqa: F401
    except Exception as e:
        return CheckResult(
            "containernet python",
            "fail",
            _short_error(e),
            "Use the Python from the Containernet venv, then install moqlab requirements there.",
            "Backend",
        )
    return CheckResult("containernet python", "ok", "mininet imports", category="Backend")


def _containernet_privilege_check() -> CheckResult:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return CheckResult(
            "containernet privileges",
            "warn",
            "current process is not root",
            "Run containernet topologies with `sudo <venv>/bin/python -m moqlab run ...`.",
            "Backend",
        )
    return CheckResult(
        "containernet privileges",
        "ok",
        "root privileges available",
        category="Backend",
    )


def _image_tags(topology: TopologyConfig | None) -> set[str]:
    if topology is None:
        return set(_DEFAULT_IMAGE_TAGS)
    return topology_image_tags(topology)


def _short_error(error: Exception) -> str:
    text = str(error).strip().splitlines()[0] if str(error).strip() else repr(error)
    lowered = text.lower()
    if "permissionerror" in lowered or "permission denied" in lowered:
        return "Docker daemon access denied"
    if "connection refused" in lowered:
        return "Docker daemon is not reachable"
    return text[:240]
