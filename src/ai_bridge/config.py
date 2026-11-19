"""Bridge settings.

Defaults live below. Override them in ~/.ai-bridge/config.json, for example:
{"gemini_model": "gemini-4-hydrogen-high", "ask_timeout_s": 300}
Every value is validated; an invalid config gives a clear error.
"""
from __future__ import annotations

import json
import re

from . import store

MODEL_RE = re.compile(r"\Agemini-[a-z0-9][a-z0-9._-]{0,60}\Z")
CLAUDE_MODEL_RE = re.compile(r"\A([a-z][a-z0-9.-]{0,60})?\Z")

DEFAULTS = {
    # Model used by gemini_ask and gemini_task (see: agy models)
    "gemini_model": "gemini-4-neon-high",
    # Max duration of one gemini_ask call, in seconds
    "ask_timeout_s": 240,
    # How deep agents may call each other (Claude -> Gemini -> Claude ...)
    "max_depth": 2,
    # Background jobs (gemini_task)
    "max_parallel_jobs": 3,
    "job_timeout_min": 20,
    # claude_ask (Gemini asking Claude). Empty model = Claude Code's default.
    "claude_model": "",
    "claude_timeout_s": 300,
    "claude_asks_per_hour": 20,
    # Commands a Gemini job may never run (on top of the network lockdown)
    "deny_commands": [
        "qm", "pct", "pvesh", "pvecm", "pveum", "vzdump",
        "zfs", "zpool", "ssh", "scp", "sftp", "rsync",
        "git push", "rm -rf", "sudo",
    ],
}

RANGES = {
    "ask_timeout_s": (10, 3600),
    "max_depth": (1, 5),
    "max_parallel_jobs": (1, 10),
    "job_timeout_min": (1, 240),
    "claude_timeout_s": (10, 3600),
    "claude_asks_per_hour": (0, 200),
}

CONFIG_FILE = store.HOME / "config.json"
CONFIG_LOCK = store.HOME / "config.lock"


def validate(cfg: dict) -> dict:
    if not isinstance(cfg.get("gemini_model"), str) or not MODEL_RE.match(cfg["gemini_model"]):
        raise ValueError("config: gemini_model must look like 'gemini-4-neon-high'.")
    if not isinstance(cfg.get("claude_model"), str) or not CLAUDE_MODEL_RE.match(cfg["claude_model"]):
        raise ValueError("config: claude_model must be empty or a model name like 'opus' or 'sonnet'.")
    for key, (lo, hi) in RANGES.items():
        v = cfg.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"config: {key} must be a whole number between {lo} and {hi}.")
    deny = cfg.get("deny_commands")
    if not isinstance(deny, list) or not all(isinstance(c, str) and c.strip() and ")" not in c for c in deny):
        raise ValueError("config: deny_commands must be a list of command strings.")
    return cfg


def _defaults() -> dict:
    return {**DEFAULTS, "deny_commands": list(DEFAULTS["deny_commands"])}


def load() -> dict:
    cfg = _defaults()
    try:
        with CONFIG_FILE.open() as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    except json.JSONDecodeError as e:
        raise ValueError(f"config.json is not valid JSON: {e}") from None
    if not isinstance(user, dict):
        raise ValueError("config.json must contain a JSON object.")
    cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    return validate(cfg)


def save(updates: dict) -> dict:
    """Validate and store user overrides (used by the UI)."""
    with store.locked(CONFIG_LOCK):
        current = {}
        try:
            current = json.loads(CONFIG_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if not isinstance(current, dict):
            current = {}
        current.update({k: v for k, v in updates.items() if k in DEFAULTS})
        validate({**_defaults(), **current})
        store.atomic_write(CONFIG_FILE, json.dumps(current, indent=2))
    return load()
