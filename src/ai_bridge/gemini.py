"""Gemini aanroepen via de Antigravity CLI (agy), headless.

- Draait in een eigen lege werkmap per thread onder ~/.ai-bridge/work/ask/.
- Altijd in plan-modus: Gemini mag lezen en nadenken, niet uitvoeren.
- Geen toegang tot je SSH-agent (SSH_AUTH_SOCK wordt verwijderd).
- Per thread wordt de conversation_id bewaard, zodat een gesprek doorloopt.
- Nooit --dangerously-skip-permissions.
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
    "Je wordt bevraagd door Claude, de orchestrator, via de ai-bridge van Dali. "
    "Dit is een overleg: geef je eigen inzicht, kritiek of alternatieve invalshoek. "
    "Voer geen acties uit en wijzig geen bestanden. "
    "Antwoord in de taal van de vraag, beknopt en concreet.\n\n"
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
    raise GeminiError("agy niet gevonden. Installeer de Antigravity CLI.")


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
    # -p moet als laatste: het neemt het volgende argument als prompt
    args += ["-p", prompt]
    if FORBIDDEN_FLAGS & set(args):
        raise GeminiError("Verboden vlag in agy-aanroep.")
    return args


def child_env() -> dict:
    env = dict(os.environ)
    env.pop("SSH_AUTH_SOCK", None)
    env["AI_BRIDGE_DEPTH"] = str(current_depth() + 1)
    return env


def parse_stream(stdout: str) -> dict:
    """Haal conversation_id, antwoord en status uit stream-json-uitvoer."""
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
        raise GeminiError("Geen resultaat ontvangen van agy.")
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
        raise GeminiError(f"Gemini antwoordde niet binnen {timeout_s} seconden.") from None

    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace")
    try:
        parsed = parse_stream(stdout)
    except GeminiError:
        tail = (stderr or stdout).strip()[-800:]
        raise GeminiError(f"agy gaf geen bruikbaar antwoord (exit {proc.returncode}). {tail}") from None

    if parsed["status"] != "SUCCESS":
        raise GeminiError(f"agy status {parsed['status']}: {parsed['response'][:800]}")
    if parsed["conversation_id"]:
        save_conversation(thread, parsed["conversation_id"])
    return parsed
