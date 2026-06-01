from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


_SKIPPED_DIRS = {".venv", ".runs"}


@dataclass(frozen=True)
class PycacheCleanupResult:
    pycache_dirs: int
    bytecode_files: int
    pytest_cache_dirs: int


def remove_pycaches(root: Path) -> PycacheCleanupResult:
    pycache_dirs = 0
    bytecode_files = 0
    pytest_cache_dirs = 0

    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if _is_skipped(path, root):
            continue
        if path.is_dir() and path.name == ".pytest_cache":
            shutil.rmtree(path)
            pytest_cache_dirs += 1
        elif path.is_dir() and path.name == "__pycache__":
            shutil.rmtree(path)
            pycache_dirs += 1
        elif path.is_file() and path.suffix in {".pyc", ".pyo"}:
            path.unlink()
            bytecode_files += 1

    return PycacheCleanupResult(
        pycache_dirs=pycache_dirs,
        bytecode_files=bytecode_files,
        pytest_cache_dirs=pytest_cache_dirs,
    )


remove_pycache = remove_pycaches


def _is_skipped(path: Path, root: Path) -> bool:
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in _SKIPPED_DIRS for part in relative_parts)
