"""Instellingen van de bridge.

Standaardwaarden staan hieronder. Overschrijven kan in ~/.ai-bridge/config.json,
bijvoorbeeld: {"gemini_model": "gemini-4-hydrogen-high", "ask_timeout_s": 300}
"""
from __future__ import annotations

import json

from . import store

DEFAULTS = {
    # Model voor gemini_ask (zie: agy models)
    "gemini_model": "gemini-4-neon-high",
    # Maximale duur van een gemini_ask-aanroep in seconden
    "ask_timeout_s": 240,
    # Hoe diep agents elkaar mogen aanroepen (Claude -> Gemini -> Claude ...)
    "max_depth": 2,
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
        raise ValueError(f"config.json is geen geldige JSON: {e}") from None
    return cfg
