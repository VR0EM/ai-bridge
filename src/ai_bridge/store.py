"""Bridge storage: the on/off switch, the shared log and small file helpers.

Everything lives in ~/.ai-bridge (mode 700, files 600), outside the repo.
The log is append-only JSONL; writes happen under a file lock.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("AI_BRIDGE_HOME", Path.home() / ".ai-bridge"))
ENABLED_FILE = HOME / "enabled"
LOG_FILE = HOME / "log.jsonl"
LOCK_FILE = HOME / "log.lock"
CURSORS_FILE = HOME / "cursors.json"
CURSORS_LOCK = HOME / "cursors.lock"

# "human" is you, writing via the `bridge note` command
AGENTS = {"claude", "gemini", "human"}
MAX_TEXT = 4000
THREAD_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class BridgeOff(Exception):
    """The bridge is switched off."""


def ensure_home() -> None:
    os.umask(0o077)
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(HOME, 0o700)


def ensure_dir(path: Path) -> Path:
    """Create a private directory (and private parents inside HOME)."""
    ensure_home()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = path
    while p != HOME and HOME in p.parents:
        os.chmod(p, 0o700)
        p = p.parent
    return path


def atomic_write(path: Path, text: str) -> None:
    """Write a file atomically with mode 600 (unique temp file + rename)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def locked(lock_path: Path, shared: bool = False):
    """Hold an flock on lock_path for the duration of the block."""
    ensure_dir(lock_path.parent)
    with open(lock_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


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
        raise BridgeOff("The bridge is off. The user can turn it on with: bridge on")


def _check_agent(agent: str) -> str:
    if agent not in AGENTS:
        raise ValueError(f"Unknown agent: {agent!r}")
    return agent


def check_thread(thread: str | None) -> str | None:
    if thread is None or thread == "":
        return None
    thread = thread.strip().lower()
    if not THREAD_RE.match(thread):
        raise ValueError("Thread name: only a-z, 0-9, - and _, max 40 characters.")
    return thread


def read_all() -> list[dict]:
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


def _last_id() -> int:
    """Id of the last log entry, reading only the end of the file."""
    if not LOG_FILE.exists():
        return 0
    with LOG_FILE.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 65536))
        lines = f.read().splitlines()
    for line in reversed(lines):
        try:
            return int(json.loads(line)["id"])
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
    return len(read_all())


def append(agent: str, text: str, thread: str | None = None) -> dict:
    """Append a message to the log and return it."""
    ensure_home()
    agent = _check_agent(agent)
    thread = check_thread(thread)
    text = text.strip()
    if not text:
        raise ValueError("Empty message.")
    if len(text) > MAX_TEXT:
        raise ValueError(f"Message too long ({len(text)} characters, max {MAX_TEXT}).")
    with locked(LOCK_FILE):
        entry = {
            "id": _last_id() + 1,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "from": agent,
            "thread": thread,
            "text": text,
        }
        fd = os.open(LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read(
    agent: str,
    only_new: bool = True,
    since_id: int = 0,
    thread: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Read messages.

    only_new=True uses and advances this agent's cursor. There is one cursor
    for "all threads" and one per thread, so reading one thread never skips
    unread messages elsewhere.
    """
    _check_agent(agent)
    thread = check_thread(thread)
    limit = max(1, min(int(limit), 200))
    key = f"{agent}:{thread or '*'}"
    with locked(CURSORS_LOCK):
        cursors = _load_cursors()
        start = cursors.get(key, 0) if only_new else max(0, int(since_id))
        entries = [e for e in read_all() if e.get("id", 0) > start]
        if thread:
            entries = [e for e in entries if e.get("thread") == thread]
        entries = entries[:limit]
        if only_new and entries:
            cursors[key] = entries[-1]["id"]
            atomic_write(CURSORS_FILE, json.dumps(cursors))
    return entries


def _load_cursors() -> dict:
    try:
        data = json.loads(CURSORS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def format_entries(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        thread = f" [{e['thread']}]" if e.get("thread") else ""
        lines.append(f"#{e['id']} {e['ts']} {e['from']}{thread}: {e['text']}")
    return "\n".join(lines)
