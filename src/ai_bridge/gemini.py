"""Calling Gemini through the Antigravity CLI (agy), headless.

Shared by gemini_ask (synchronous) and the background workers:
- Every run gets the cage from sandbox.py (terminal sandbox, black-hole proxy,
  deny list) through a per-run --settings file.
- agy gets a minimal environment (allowlist), so tokens or keys in your
  shell environment never reach it. No SSH agent.
- agy runs in its own process group, so a timeout or cancel kills everything it started.
- Never --dangerously-skip-permissions.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
from pathlib import Path

from . import config, sandbox, store

WORK_DIR = store.HOME / "work" / "ask"
THREADS_FILE = store.HOME / "threads.json"
THREADS_LOCK = store.HOME / "threads.lock"

FORBIDDEN_FLAGS = ("--dangerously-skip-permissions",)
CONV_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]{7,63}\Z")
MAX_PROMPT = 20000

ENV_ALLOW = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM",
             "TZ", "__CF_USER_TEXT_ENCODING"}
ENV_ALLOW_PREFIXES = ("LC_", "XDG_")

ASK_PREAMBLE = (
    "You are being consulted by Claude, the orchestrator, through the user's ai-bridge. "
    "This is a discussion: give your own insight, criticism or alternative angle. "
    "Do not take actions, do not run commands and do not modify files. "
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


def check_model(model: str) -> str:
    if not isinstance(model, str) or not config.MODEL_RE.match(model):
        raise ValueError("Only gemini models are allowed, e.g. gemini-4-neon-high (see: agy models).")
    return model


def check_prompt(text: str, what: str = "Prompt") -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError(f"{what} is empty.")
    if len(text) > MAX_PROMPT:
        raise ValueError(f"{what} too long ({len(text)} characters, max {MAX_PROMPT}).")
    return text


def _load_threads() -> dict:
    try:
        data = json.loads(THREADS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_conversation(thread: str) -> str | None:
    with store.locked(THREADS_LOCK, shared=True):
        conv = _load_threads().get(thread)
    return conv if isinstance(conv, str) and CONV_RE.match(conv) else None


def save_conversation(thread: str, conversation_id: str | None) -> None:
    with store.locked(THREADS_LOCK):
        data = _load_threads()
        if conversation_id and CONV_RE.match(conversation_id):
            data[thread] = conversation_id
        else:
            data.pop(thread, None)
        store.atomic_write(THREADS_FILE, json.dumps(data, indent=2))


def child_env(depth: int | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)}
    env["AI_BRIDGE_DEPTH"] = str((current_depth() if depth is None else depth) + 1)
    # Anything agy starts (including its own ai-bridge MCP server) speaks as Gemini
    env["AI_BRIDGE_AGENT"] = "gemini"
    if "AI_BRIDGE_HOME" in os.environ:
        env["AI_BRIDGE_HOME"] = os.environ["AI_BRIDGE_HOME"]
    return env


def build_args(prompt: str, *, model: str, timeout_s: int, settings_file: Path,
               mode: str = "plan", conversation_id: str | None = None) -> list[str]:
    args = [
        agy_path(),
        "--output-format", "stream-json",
        "--mode", mode,
        "--sandbox",
        "--settings", str(settings_file),
        "--model", check_model(model),
        "--print-timeout", f"{int(timeout_s)}s",
    ]
    if conversation_id:
        if not CONV_RE.match(conversation_id):
            raise GeminiError("Invalid conversation id.")
        args += ["--conversation", conversation_id]
    # -p must come last: it takes the next argument as the prompt.
    # The prompt never starts with '-', so it cannot be mistaken for a flag.
    if prompt.startswith("-"):
        prompt = " " + prompt
    args += ["-p", prompt]
    if any(a.startswith(FORBIDDEN_FLAGS) for a in args[:-1]):
        raise GeminiError("Forbidden flag in agy call.")
    return args


def parse_stream(stdout: str) -> dict:
    """Extract conversation_id, response, status and error from stream-json output."""
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
        "error": result.get("error"),
        "usage": result.get("usage") or {},
        "duration_s": result.get("duration_seconds"),
    }


def outcome(parsed: dict) -> tuple[str, str | None]:
    """Decide what a run produced: ('ok', None), ('partial', warning) or raise.

    agy sometimes reports ERROR after the full answer was already streamed
    (e.g. the connection dropped while the Mac went to sleep). An answer is
    never thrown away: it is returned with a warning.
    """
    if parsed["status"] == "SUCCESS" and parsed["response"]:
        return "ok", None
    if parsed["response"]:
        return "partial", f"agy reported {parsed['status']}: {parsed.get('error') or 'unknown error'}"
    raise GeminiError(f"agy status {parsed['status']}: {parsed.get('error') or 'no answer'}")


def kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def _prepare(question: str, thread: str, model: str, timeout_s: int,
             deny_commands: list[str]) -> tuple[list[str], Path]:
    workdir = store.ensure_dir(WORK_DIR / thread)
    settings_file = sandbox.write_settings(
        WORK_DIR / f"{thread}.settings.json",
        sandbox.agy_settings(deny_commands, read_only=True, web=True),
    )
    conversation_id = get_conversation(thread)
    prompt = ("Follow-up question:\n" + question) if conversation_id else ASK_PREAMBLE + question
    args = build_args(prompt, model=model, timeout_s=timeout_s,
                      settings_file=settings_file, conversation_id=conversation_id)
    return args, workdir


async def _stop(proc) -> None:
    kill_group(proc.pid)
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(asyncio.shield(proc.wait()), 5)  # reap: no zombies


async def ask(question: str, thread: str, model: str, timeout_s: int, deny_commands: list[str]) -> dict:
    # File and lock work runs off the event loop
    args, workdir = await asyncio.to_thread(_prepare, question, thread, model, timeout_s, deny_commands)
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=workdir, env=child_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    task = asyncio.ensure_future(proc.communicate())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s + 30
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=2)
            if done:
                out, err = task.result()
                break
            if not store.is_enabled():  # the kill switch also stops a running ask
                await _stop(proc)
                raise store.BridgeOff("The bridge was switched off while Gemini was answering.")
            if loop.time() > deadline:
                await _stop(proc)
                raise GeminiError(f"Gemini did not answer within {timeout_s} seconds.")
    except BaseException:
        # Cancelled by the client (or any error): never leave agy running
        task.cancel()
        await _stop(proc)
        raise

    stdout = out.decode("utf-8", errors="replace")
    stderr = err.decode("utf-8", errors="replace")
    try:
        parsed = parse_stream(stdout)
    except GeminiError:
        tail = (stderr or stdout).strip()[-800:]
        raise GeminiError(f"agy returned no usable answer (exit {proc.returncode}). {tail}") from None

    _, warning = outcome(parsed)
    await asyncio.to_thread(save_conversation, thread, parsed["conversation_id"])
    parsed["warning"] = warning
    return parsed
