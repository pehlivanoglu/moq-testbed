"""Regression checks for Chromium media runner launch behavior."""

from pathlib import Path


def test_runner_reuses_initial_chromium_page():
    runner = (Path(__file__).parents[2] / "docker" / "media-runner.mjs").read_text()

    assert 'endpoint("/json/list")' in runner
    assert "/json/new" not in runner


def test_runner_writes_atomic_player_metrics():
    runner = (Path(__file__).parents[2] / "docker" / "media-runner.mjs").read_text()

    assert "__moqlabPlayerMetrics" in runner
    assert 'renameSync(temporary, path)' in runner
    assert 'const metricsPath = "/tmp/moqlab-player-metrics.json"' in runner
