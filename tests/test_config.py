"""Harness config: dotenv parsing edge cases and env-var fallbacks."""

from __future__ import annotations

import os
from pathlib import Path

from trace_harness.config import HarnessConfig, load_env_file


def test_dotenv_parsing_handles_real_world_syntax(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(
        "﻿ZZQ_BOM_KEY=1\n"
        "export ZZQ_EXPORTED=abc\n"
        "ZZQ_COMMENTED=./runs # artifacts here\n"
        'ZZQ_QUOTED="quoted # not a comment"\n'.encode()
    )
    applied = load_env_file(env)
    try:
        assert applied == {
            "ZZQ_BOM_KEY": "1",
            "ZZQ_EXPORTED": "abc",
            "ZZQ_COMMENTED": "./runs",
            "ZZQ_QUOTED": "quoted # not a comment",
        }
    finally:
        for key in applied:
            os.environ.pop(key, None)


def test_shell_env_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("ZZQ_PRESET", "shell")
    (tmp_path / ".env").write_text("ZZQ_PRESET=file\n")
    load_env_file(tmp_path / ".env")
    assert os.environ["ZZQ_PRESET"] == "shell"


def test_empty_env_values_fall_back_to_defaults(monkeypatch):
    """TRACE_RUNS_DIR= must not become Path('') == cwd."""
    monkeypatch.setenv("TRACE_RUNS_DIR", "")
    monkeypatch.setenv("TRACE_MODEL_PROVIDER", "")
    monkeypatch.setenv("TRACE_LOG_LEVEL", "")
    config = HarnessConfig.from_env()
    assert config.runs_dir == Path("./runs")
    assert config.model_provider == "fixture"
    assert config.log_level == "INFO"
