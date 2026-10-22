from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

import yaml

from moqlab.exceptions import ConfigError, MoqlabError
from simulation.schema import SimulationConfig, document, resolved_config

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / ".build"
BINARY = BUILD / "native" / "moqlab-sim"


def execute(argv: list[str], *, cwd: Path | None = None, log: Path | None = None) -> None:
    try:
        if log:
            with log.open("w") as stream:
                subprocess.run(argv, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT, check=True)
        else:
            subprocess.run(argv, cwd=cwd, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise MoqlabError(f"command failed: {' '.join(argv)}" + (f"; see {log}" if log else "")) from error


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def lock_sources() -> dict[str, object]:
    lock = json.loads((ROOT / "deps.lock.json").read_text())
    for name, spec in lock.items():
        if name == "json":
            dest = BUILD / "include" / "nlohmann" / "json.hpp"
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with urlopen(spec["url"], timeout=60) as response:
                    dest.write_bytes(response.read())
            if digest(dest) != spec["sha256"]:
                raise ConfigError("JSON header hash mismatch")
            continue
        if name == "picotls":
            continue  # picoquic FetchContent uses the locked revision below.
        dest = BUILD / name
        if not dest.exists():
            execute(["git", "init", str(dest)])
            execute(["git", "remote", "add", "origin", spec["url"]], cwd=dest)
            execute(["git", "fetch", "--depth", "1", "origin", spec["commit"]], cwd=dest)
            execute(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=dest)
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=dest, text=True).strip()
        if actual != spec["commit"]:
            raise ConfigError(f"{name}: source revision differs from lock; refusing to overwrite")
    return lock


def apply_patch(repo: Path, patch: Path) -> None:
    if subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=repo, capture_output=True).returncode == 0:
        return
    execute(["git", "apply", "--check", str(patch)], cwd=repo)
    execute(["git", "apply", str(patch)], cwd=repo)


def build(jobs: int) -> None:
    BUILD.mkdir(exist_ok=True)
    lock = lock_sources()
    patches = sorted((BUILD / "stratum" / "patches" / "ns3").glob("*.patch"))
    patch_hashes = {p.name: digest(p) for p in patches}
    patch_state = BUILD / "ns3-patches.json"
    if patch_state.exists():
        state = json.loads(patch_state.read_text())
        if state["patches"] != patch_hashes or any(digest(BUILD / "ns3" / name) != value for name, value in state["files"].items()):
            raise ConfigError("ns-3 patch/source integrity mismatch; refusing to overwrite")
    else:
        # Patches overlap: validate the final tree, rather than reversing each patch in isolation.
        for patch in patches:
            execute(["git", "apply", "--check", str(patch)], cwd=BUILD / "ns3")
            execute(["git", "apply", str(patch)], cwd=BUILD / "ns3")
        targets = {name for patch in patches for name in re.findall(r"^\+\+\+ b/(.+)$", patch.read_text(), flags=re.M)}
        patch_state.write_text(json.dumps({"patches": patch_hashes, "files": {name: digest(BUILD / "ns3" / name) for name in targets}}, indent=2) + "\n")
    module = BUILD / "ns3" / "contrib" / "stratum"
    if not module.exists():
        module.symlink_to(BUILD / "stratum", target_is_directory=True)
    elif module.resolve() != BUILD / "stratum":
        raise ConfigError("unexpected stratum contrib path")
    for patch in sorted((ROOT / "patches").glob("picoquic-*.patch")):
        apply_patch(BUILD / "picoquic", patch)
    for name, options in [
        ("ns3", ["-DNS3_TESTS=ON", "-DNS3_EXAMPLES=OFF", "-DNS3_ASSERT=ON", "-DNS3_WARNINGS_AS_ERRORS=OFF",
                 "-DNS3_ENABLED_MODULES=core;network;internet;point-to-point;csma;bridge;traffic-control;applications;flow-monitor;stratum"]),
        ("picoquic", ["-DPICOQUIC_FETCH_PTLS=ON", f"-DPICOQUIC_FETCH_PTLS_TAG={lock['picotls']['commit']}",
                      "-Dpicoquic_BUILD_TESTS=ON", "-DBUILD_DEMO=OFF", "-DBUILD_HTTP=ON", "-DBUILD_PQBENCH=OFF", "-DBUILD_PICO_SIM=OFF"]),
    ]:
        dest = BUILD / ("ns3-build" if name == "ns3" else "pico-build")
        execute(["cmake", "-S", str(BUILD / name), "-B", str(dest), "-DCMAKE_BUILD_TYPE=Release", *options], log=BUILD / f"{name}-configure.log")
        execute(["cmake", "--build", str(dest), "-j", str(jobs)], log=BUILD / f"{name}-build.log")
    execute(["cmake", "--install", str(BUILD / "ns3-build"), "--prefix", str(BUILD / "ns3-install")], log=BUILD / "ns3-install.log")
    execute(["cmake", "-S", str(ROOT), "-B", str(BUILD / "native"), "-DCMAKE_BUILD_TYPE=Release"], log=BUILD / "native-configure.log")
    execute(["cmake", "--build", str(BUILD / "native"), "-j", str(jobs)], log=BUILD / "native-build.log")
    (BUILD / "build.json").write_text(json.dumps({"dependencies": lock, "binary_sha256": digest(BINARY), "platform": platform.platform()}, indent=2) + "\n")


def provenance() -> dict[str, object]:
    repo = ROOT.parents[1]
    paths = [*ROOT.glob("*.py"), *ROOT.glob("*.json"), ROOT / "CMakeLists.txt", *ROOT.glob("native/*"), *ROOT.glob("patches/*"),
             *repo.glob("src/sbd/*"), *ROOT.parent.glob("moqlab/config/*.py")]
    hashes = {str(path.relative_to(repo)): digest(path) for path in sorted(paths) if path.is_file()}
    return {"git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
            "git_status": subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True),
            "source_sha256": hashes, "binary_sha256": digest(BINARY),
            "dependencies": json.loads((ROOT / "deps.lock.json").read_text()),
            "patch_sha256": {p.name: digest(p) for p in sorted((BUILD / "stratum/patches/ns3").glob("*.patch"))},
            "platform": platform.platform(), "python": sys.version,
            "logical_cpus": os.cpu_count(),
            "python_dependencies": {name: importlib.metadata.version(name) for name in ("pydantic", "click", "PyYAML")},
            "dependency_diff_sha256": {name: hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"], cwd=BUILD / name)).hexdigest()
                                       for name in ("ns3", "picoquic", "stratum")},
            "assumptions": {"clock": "ideal shared virtual clock", "jitter": "independent uniform, clipped at zero; reordering allowed",
                            "quic_bbr": "picoquic bbr.c (not bbr1.c), exact source revision recorded",
                            "tls": "real encryption, fixture certificate; verification disabled inside closed simulator",
                            "l4s": "ECT(1) classification is not controller L4S conformance",
                            "feedback": "reliable QUIC stream 2; packet timestamps, not a standard ACK extension",
                            "tcp": "ns-3 TcpCubic, ECN enabled"}}


def archive_native_sources(root: Path) -> None:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    repo = ROOT.parents[1]
    paths = [path for path in manifest["source_sha256"]
             if path.startswith("src/sbd/") or path.startswith("moqlab/simulation/native/")
             or path.startswith("moqlab/simulation/patches/")
             or path in {"moqlab/simulation/CMakeLists.txt", "moqlab/simulation/deps.lock.json"}]
    for name in paths:
        if digest(repo / name) != manifest["source_sha256"][name]:
            raise ConfigError(f"cannot archive changed native source: {name}")
    archive = root / "native-sources.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
        for name in paths:
            stream.write(repo / name, name)
    manifest["native_source_archive_sha256"] = digest(archive)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")


def run(config: SimulationConfig, source: Path, destination: Path | None = None, workers: int = 1) -> Path:
    if not BINARY.exists():
        raise ConfigError("simulation is not built; run python -m simulation build")
    native_inputs = [*ROOT.glob("native/*"), *ROOT.parents[1].glob("src/sbd/*"), ROOT / "CMakeLists.txt"]
    if any(path.is_file() and path.stat().st_mtime_ns > BINARY.stat().st_mtime_ns for path in native_inputs):
        raise ConfigError("native sources changed; rebuild with python -m simulation build")
    resolved = resolved_config(config)
    root = destination or ROOT / ".runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise ConfigError(f"refusing to overwrite run directory {root}") from error
    (root / "input.yaml").write_text(source.read_text())
    (root / "resolved.yaml").write_text(yaml.safe_dump(document(config), sort_keys=False))
    manifest = provenance()
    manifest.update({"status": "running", "created_utc": datetime.now(timezone.utc).isoformat(), "workers": workers, "runs": []})
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    def repetition(number: int) -> dict[str, object]:
        directory = root / f"run-{number:04d}"
        directory.mkdir()
        payload = {**resolved, "run_number": number}
        (directory / "resolved.json").write_text(json.dumps(payload, indent=2) + "\n")
        argv = [str(BINARY), str(directory / "resolved.json"), str(directory), str(BUILD / "picoquic" / "certs")]
        before = time.monotonic()
        with (directory / "stderr.log").open("w") as log:
            # GNU time reports per-process RSS, unlike cumulative RUSAGE_CHILDREN.
            command = ["/usr/bin/time", "-f", "%M", "-o", str(directory / "peak-rss-kib.txt"), *argv] if Path("/usr/bin/time").exists() else argv
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
        if not (directory / "COMPLETE").exists():
            raise ConfigError(f"native run did not complete: {directory}")
        item = {"run_number": number, "seed": config.experiment.seed, "wall_seconds": time.monotonic() - before,
                "command": argv, "output_bytes": sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())}
        if (directory / "peak-rss-kib.txt").exists():
            item["peak_rss_kib"] = int((directory / "peak-rss-kib.txt").read_text())
        return item
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = [pool.submit(repetition, number) for number in range(1, config.experiment.repetitions + 1)]
            for future in as_completed(pending):
                manifest["runs"].append(future.result())
                manifest["runs"].sort(key=lambda item: item["run_number"])
                manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest["status"] = "complete"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        archive_native_sources(root)
        (root / "COMPLETE").write_text("all repetitions completed\n")
    except (OSError, ValueError, subprocess.CalledProcessError, MoqlabError) as error:
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        raise MoqlabError(f"simulation failed; partial output retained at {root}: {error}") from error
    return root


def doctor() -> list[tuple[str, bool, str]]:
    checks = [(name, shutil.which(name) is not None, shutil.which(name) or "missing") for name in ["git", "cmake", "g++", "openssl"]]
    checks.append(("simulation binary", BINARY.exists(), str(BINARY)))
    checks.append(("Docker/Containernet/root", True, "not required"))
    return checks
