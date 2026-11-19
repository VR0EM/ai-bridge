"""Bridge settings.

Defaults live below. Override them in ~/.ai-bridge/config.json, for example:
{"gemini_model": "gemini-4-hydrogen-high", "ask_timeout_s": 300}
"""
from __future__ import annotations

import json

from . import store

DEFAULTS = {
    # Model used by gemini_ask (see: agy models)
    "gemini_model": "gemini-4-neon-high",
    # Max duration of one gemini_ask call, in seconds
    "ask_timeout_s": 240,
    # How deep agents may call each other (Claude -> Gemini -> Claude ...)
    "max_depth": 2,
    # Background jobs (gemini_task)
    "max_parallel_jobs": 3,
    "job_timeout_min": 20,
    # Commands a Gemini job may never run (on top of the network lockdown)
    "deny_commands": [
        "qm", "pct", "pvesh", "pvecm", "pveum", "vzdump",
        "zfs", "zpool", "ssh", "scp", "sftp", "rsync",
        "git push", "rm -rf", "sudo",
    ],
}

CONFIG_FILE = store.HOME / "config.json"


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with CONFIG_FILE.open() as f:
            user = json.load(f)
        cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as e:
        raise ValueError(f"config.json is not valid JSON: {e}") from None
    return cfg
