"""Calling Gemini through the Antigravity CLI (agy), headless.

- Runs in its own empty working directory per thread under ~/.ai-bridge/work/ask/.
- Always in plan mode: Gemini may read and think, not execute.
- No access to your SSH agent (SSH_AUTH_SOCK is removed).
- The conversation_id is stored per thread, so a conversation continues.
- Never --dangerously-skip-permissions.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
from pathlib import Path

from . import store

WORK_DIR = store.HOME / "work" / "ask"
THREADS_FILE = store.HOME / "threads.json"
THREADS_LOCK = store.HOME / "threads.lock"

FORBIDDEN_FLAGS = {"--dangerously-skip-permissions"}

ASK_PREAMBLE = (
    "You are being consulted by Claude, the orchestrator, through the user's ai-bridge. "
    "This is a discussion: give your own insight, criticism or alternative angle. "
    "Do not take actions and do not modify files. "
    "Answer in the language of the question, concise and concrete.\n\n"
)


class GeminiError(Exception):
    pass


def agy_path() -> str:
    found = shutil.which("agy")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "agy"
    if fallback.exists():
        return str(fallback)
    raise GeminiError("agy not found. Install the Antigravity CLI.")


def current_depth() -> int:
    try:
        return int(os.environ.get("AI_BRIDGE_DEPTH", "0"))
    except ValueError:
        return 0


def _load_threads() -> dict:
    try:
        return json.loads(THREADS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_conversation(thread: str) -> str | None:
    return _load_threads().get(thread)


def save_conversation(thread: str, conversation_id: str) -> None:
    store.ensure_home()
    with THREADS_LOCK.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = _load_threads()
            data[thread] = conversation_id
            THREADS_FILE.write_text(json.dumps(data, indent=2))
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def build_args(prompt: str, model: str, timeout_s: int, conversation_id: str | None) -> list[str]:
    args = [
        agy_path(),
        "--output-format", "stream-json",
        "--mode", "plan",
        "--model", model,
        "--print-timeout", f"{timeout_s}s",
    ]
    if conversation_id:
        args += ["--conversation", conversation_id]
    # -p must come last: it takes the next argument as the prompt
    args += ["-p", prompt]
    if FORBIDDEN_FLAGS & set(args):
        raise GeminiError("Forbidden flag in agy call.")
    return args


def child_env() -> dict:
    env = dict(os.environ)
    env.pop("SSH_AUTH_SOCK", None)
    env["AI_BRIDGE_DEPTH"] = str(current_depth() + 1)
    # Anything agy starts (including its own ai-bridge MCP server) speaks as Gemini
    env["AI_BRIDGE_AGENT"] = "gemini"
    return env


def parse_stream(stdout: str) -> dict:
    """Extract conversation_id, response and status from stream-json output."""
    conversation_id = None
    result = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "init":
            conversation_id = event.get("conversation_id") or conversation_id
        elif event.get("event") == "result":
            result = event.get("result", {})
            conversation_id = result.get("conversation_id") or conversation_id
    if result is None:
        raise GeminiError("No result received from agy.")
    return {
        "conversation_id": conversation_id,
        "status": result.get("status"),
        "response": (result.get("response") or "").strip(),
        "usage": result.get("usage", {}),
        "duration_s": result.get("duration_seconds"),
    }


async def ask(question: str, thread: str, model: str, timeout_s: int) -> dict:
    workdir = WORK_DIR / thread
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    conversation_id = get_conversation(thread)
    prompt = question if conversation_id else ASK_PREAMBLE + question
    args = build_args(prompt, model, timeout_s, conversation_id)

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=workdir,
        env=child_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise GeminiError(f"Gemini did not answer within {timeout_s} seconds.") from None

    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace")
    try:
        parsed = parse_stream(stdout)
    except GeminiError:
        tail = (stderr or stdout).strip()[-800:]
        raise GeminiError(f"agy returned no usable answer (exit {proc.returncode}). {tail}") from None

    if parsed["status"] != "SUCCESS":
        raise GeminiError(f"agy status {parsed['status']}: {parsed['response'][:800]}")
    if parsed["conversation_id"]:
        save_conversation(thread, parsed["conversation_id"])
    return parsed
