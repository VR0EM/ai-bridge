import json

import pytest

from ai_bridge import config


def write(cfg):
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(cfg if isinstance(cfg, str) else json.dumps(cfg))


def test_defaults_are_valid():
    assert config.load()["gemini_model"].startswith("gemini-")


def test_override():
    write({"gemini_model": "gemini-4-hydrogen-high", "unknown": 1})
    cfg = config.load()
    assert cfg["gemini_model"] == "gemini-4-hydrogen-high"
    assert "unknown" not in cfg


@pytest.mark.parametrize("bad", [
    "{not json", "[1, 2]",
    {"max_parallel_jobs": 0}, {"max_parallel_jobs": "3"}, {"max_depth": True},
    {"job_timeout_min": -5}, {"gemini_model": "claude-opus-5-5-high"},
    {"gemini_model": "gemini-x --dangerously-skip-permissions"},
    {"deny_commands": "ssh"}, {"deny_commands": ["ok", "bad)"]},
])
def test_invalid_config_is_rejected(bad):
    write(bad)
    with pytest.raises(ValueError):
        config.load()


def test_save_validates_and_persists():
    with pytest.raises(ValueError):
        config.save({"max_parallel_jobs": 99})
    assert config.save({"max_parallel_jobs": 2})["max_parallel_jobs"] == 2
