"""Unit tests for local cleanup helpers."""

from __future__ import annotations

from pathlib import Path

from moqlab.cleanup import remove_pycache


def test_remove_pycache_deletes_project_cache_dirs_and_bytecode(tmp_path: Path):
    root = tmp_path / "moqlab"
    cache_dir = root / "moqlab" / "__pycache__"
    cache_dir.mkdir(parents=True)
    pyc = cache_dir / "cli.cpython-312.pyc"
    pyc.write_bytes(b"cache")

    standalone = root / "tests" / "unit" / "leftover.pyc"
    standalone.parent.mkdir(parents=True)
    standalone.write_bytes(b"cache")

    result = remove_pycache(root)

    assert result.pycache_dirs == 1
    assert result.bytecode_files == 2
    assert result.pytest_cache_dirs == 0
    assert not cache_dir.exists()
    assert not standalone.exists()


def test_remove_pycache_deletes_pytest_cache(tmp_path: Path):
    root = tmp_path / "moqlab"
    pytest_cache = root / ".pytest_cache"
    pytest_cache.mkdir(parents=True)
    (pytest_cache / "README.md").write_text("pytest cache")

    result = remove_pycache(root)

    assert result.pycache_dirs == 0
    assert result.bytecode_files == 0
    assert result.pytest_cache_dirs == 1
    assert not pytest_cache.exists()


def test_remove_pycache_skips_venv_and_runtime_dirs(tmp_path: Path):
    root = tmp_path / "moqlab"
    skipped_paths = [
        root / ".venv" / "lib" / "__pycache__" / "site.cpython-312.pyc",
        root / ".runs" / "run_1" / "__pycache__" / "x.cpython-312.pyc",
    ]
    for path in skipped_paths:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"cache")

    result = remove_pycache(root)

    assert result.pycache_dirs == 0
    assert result.bytecode_files == 0
    assert result.pytest_cache_dirs == 0
    for path in skipped_paths:
        assert path.exists()
