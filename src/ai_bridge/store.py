"""Opslag van de bridge: aan/uit-schakelaar en het gedeelde logboek.

Alles staat in ~/.ai-bridge (rechten 700, bestanden 600).
Het logboek is append-only JSONL; schrijven gebeurt onder een bestandslock.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("AI_BRIDGE_HOME", Path.home() / ".ai-bridge"))
ENABLED_FILE = HOME / "enabled"
LOG_FILE = HOME / "log.jsonl"
LOCK_FILE = HOME / "log.lock"

AGENTS = {"claude", "gemini", "dali"}
MAX_TEXT = 4000
THREAD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class BridgeOff(Exception):
    """De bridge staat uit."""


def ensure_home() -> None:
    os.umask(0o077)
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)


def is_enabled() -> bool:
    return ENABLED_FILE.exists()


def set_enabled(on: bool) -> None:
    ensure_home()
    if on:
        ENABLED_FILE.touch()
    else:
        ENABLED_FILE.unlink(missing_ok=True)


def require_on() -> None:
    if not is_enabled():
        raise BridgeOff("De bridge staat uit. Dali kan ze aanzetten met: bridge on")


def _check_agent(agent: str) -> str:
    if agent not in AGENTS:
        raise ValueError(f"Onbekende agent: {agent!r}")
    return agent


def _check_thread(thread: str | None) -> str | None:
    if thread is None or thread == "":
        return None
    thread = thread.strip().lower()
    if not THREAD_RE.match(thread):
        raise ValueError("Threadnaam: enkel a-z, 0-9, - en _, max 40 tekens.")
    return thread


def _read_all() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    entries = []
    with LOG_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries


def append(agent: str, text: str, thread: str | None = None) -> dict:
    """Voeg een bericht toe aan het logboek en geef het terug."""
    ensure_home()
    agent = _check_agent(agent)
    thread = _check_thread(thread)
    text = text.strip()
    if not text:
        raise ValueError("Leeg bericht.")
    if len(text) > MAX_TEXT:
        raise ValueError(f"Bericht te lang ({len(text)} tekens, max {MAX_TEXT}).")
    with LOCK_FILE.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            entries = _read_all()
            next_id = (entries[-1]["id"] + 1) if entries else 1
            entry = {
                "id": next_id,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "from": agent,
                "thread": thread,
                "text": text,
            }
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.chmod(LOG_FILE, 0o600)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return entry


def _cursor_file(agent: str) -> Path:
    return HOME / f"cursor-{_check_agent(agent)}"


def get_cursor(agent: str) -> int:
    try:
        return int(_cursor_file(agent).read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def set_cursor(agent: str, value: int) -> None:
    ensure_home()
    _cursor_file(agent).write_text(str(value))


def read(
    agent: str,
    only_new: bool = True,
    since_id: int = 0,
    thread: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Lees berichten. only_new=True gebruikt en verschuift de cursor van deze agent."""
    thread = _check_thread(thread)
    limit = max(1, min(limit, 200))
    start = get_cursor(agent) if only_new else max(0, since_id)
    entries = [e for e in _read_all() if e.get("id", 0) > start]
    if thread:
        entries = [e for e in entries if e.get("thread") == thread]
    entries = entries[:limit]
    if only_new and entries:
        set_cursor(agent, entries[-1]["id"])
    return entries


def format_entries(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        thread = f" [{e['thread']}]" if e.get("thread") else ""
        lines.append(f"#{e['id']} {e['ts']} {e['from']}{thread}: {e['text']}")
    return "\n".join(lines)
