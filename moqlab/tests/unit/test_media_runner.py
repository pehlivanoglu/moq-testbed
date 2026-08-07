"""Regression checks for Chromium media runner launch behavior."""

from pathlib import Path


def test_runner_reuses_initial_chromium_page():
    runner = (Path(__file__).parents[2] / "docker" / "media-runner.mjs").read_text()

    assert 'endpoint("/json/list")' in runner
    assert "/json/new" not in runner
