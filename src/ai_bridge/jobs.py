"""Background Gemini jobs (gemini_task).

Each job is a detached worker process (python -m ai_bridge.worker <id>), so it
keeps running when the MCP client restarts. Job state lives in
~/.ai-bridge/jobs/<id>/job.json; the answer in result.md.

Robustness rules:
- Every read-modify-write of job.json happens under the per-job lock
  (job.lock), and files are replaced atomically.
- Liveness is not checked with PIDs (zombies and PID reuse lie). The worker
  holds an exclusive lock on alive.lock for its whole life; if that lock can be
  taken, the worker is gone.
- Concurrency: a worker must hold one of N slot locks before it starts agy.
  Locks are released automatically when a process dies.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import store

JOBS_DIR = store.HOME / "jobs"
SLOTS_DIR = store.HOME / "slots"
JOB_ID_RE_CHARS = set("0123456789abcdef")
ACTIVE = {"queued", "running"}
FINAL = {"done", "failed", "cancelled"}
START_GRACE = timedelta(seconds=30)

ROLES = {"researcher", "reviewer"}

# A reviewer may only look at normal folders inside the home folder.
# Hidden top-level folders (.ssh, .gemini, .aws, ...) are always refused.
SENSITIVE_TOP = {"library", "applications"}

ROLE_PROMPTS = {
    "researcher": (
        "You are a research subagent working for Claude (the orchestrator) through the "
        "user's ai-bridge. Research the task below thoroughly. You may search and read "
        "web pages. Do not modify any files and do not run shell commands.\n"
        "Deliver a concise report in Markdown with: a short answer first, then the key "
        "findings, the sources you used (with URLs), and what remains uncertain.\n\n"
        "Task:\n"
    ),
    "reviewer": (
        "You are a review subagent working for Claude (the orchestrator) through the "
        "user's ai-bridge. The current directory contains the code or files to review. "
        "Read what you need, only inside the current directory. Do not modify any files "
        "and do not run shell commands.\n"
        "Deliver a review in Markdown with: a one-paragraph summary, a list of issues "
        "(severity: high/medium/low, file and line where possible, why it matters, how to "
        "fix), and a last line that is exactly 'VERDICT: APPROVE' or "
        "'VERDICT: CHANGES_REQUESTED'.\n\n"
        "Review request:\n"
    ),
}

# Popen objects of workers started by this process, so they get reaped
_children: list[subprocess.Popen] = []
_children_lock = threading.Lock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not 8 <= len(job_id) <= 32 or not set(job_id) <= JOB_ID_RE_CHARS:
        raise ValueError("Invalid job id.")
    return job_id


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / check_id(job_id)


def _lock(job_id: str):
    return store.locked(job_dir(job_id) / "job.lock")


def _read(job_id: str) -> dict:
    try:
        return json.loads((job_dir(job_id) / "job.json").read_text())
    except FileNotFoundError:
        raise ValueError(f"Unknown job: {job_id}") from None


def _write(job: dict) -> None:
    store.atomic_write(job_dir(job["id"]) / "job.json", json.dumps(job, indent=2))


def load(job_id: str) -> dict:
    with _lock(job_id):
        return _read(job_id)


def update(job_id: str, only_if_active: bool = False, **fields) -> dict:
    """Atomically update fields. With only_if_active, a final status is never overwritten."""
    with _lock(job_id):
        job = _read(job_id)
        if only_if_active and job["status"] in FINAL:
            return job
        job.update(fields)
        _write(job)
        return job


def worker_alive(job_id: str) -> bool:
    """True while the worker process holds alive.lock."""
    path = job_dir(job_id) / "alive.lock"
    if not path.exists():
        return False
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


def reap() -> None:
    """Collect exited worker processes started by this process (no zombies)."""
    with _children_lock:
        _children[:] = [p for p in _children if p.poll() is None]


def refresh(job: dict) -> dict:
    """Mark jobs whose worker is gone as failed."""
    if job["status"] not in ACTIVE:
        return job
    if worker_alive(job["id"]):
        return job
    created = datetime.fromisoformat(job["created"])
    if not job.get("worker_started") and datetime.now(timezone.utc) - created < START_GRACE:
        return job  # worker is still starting up
    return update(job["id"], only_if_active=True, status="failed", finished=now(),
                  error="Worker process stopped unexpectedly.")


def check_review_path(path: str | None) -> str:
    if not path:
        raise ValueError("A reviewer needs a 'path' to the folder to review.")
    p = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    if not p.is_dir():
        raise ValueError(f"Not a folder: {p}")
    if p == home or home not in p.parents:
        raise ValueError("The review path must be a folder inside your home folder (not the home folder itself).")
    top = p.relative_to(home).parts[0]
    # macOS file systems are usually case-insensitive: compare case-folded
    if top.startswith(".") or top.casefold() in SENSITIVE_TOP:
        raise ValueError(f"Refusing to review a hidden or system folder: ~/{top}")
    bridge_home = store.HOME.resolve()
    if bridge_home == p or bridge_home in p.parents or p in bridge_home.parents:
        raise ValueError("Refusing to review the bridge's own data folder (or a folder containing it).")
    return str(p)


def create(role: str, task: str, path: str | None, model: str, thread: str | None,
           requested_by: str, depth: int, timeout_min: int) -> dict:
    if role not in ROLES:
        raise ValueError(f"Unknown role {role!r}. Choose from: {', '.join(sorted(ROLES))}.")
    cwd = check_review_path(path) if role == "reviewer" else None

    store.ensure_dir(JOBS_DIR)
    while True:
        job_id = secrets.token_hex(8)
        try:
            (JOBS_DIR / job_id).mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    d = job_dir(job_id)
    workdir = store.ensure_dir(d / "workdir")
    job = {
        "id": job_id, "role": role, "task": task, "path": cwd,
        "cwd": cwd or str(workdir), "model": model, "thread": thread,
        "requested_by": requested_by, "depth": depth, "timeout_min": timeout_min,
        "status": "queued", "created": now(), "started": None, "finished": None,
        "pid": None, "worker_started": False, "error": None, "warning": None, "usage": None,
    }
    reap()
    # Hold the job lock until the pid is recorded: the worker waits for it.
    with _lock(job_id):
        _write(job)
        try:
            with open(d / "worker.log", "ab") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "ai_bridge.worker", job_id],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True,  # own process group: survives client restarts
                    env={**os.environ, "AI_BRIDGE_HOME": str(store.HOME)},
                )
        except OSError as e:
            job.update(status="failed", finished=now(), error=f"Could not start worker: {e}")
            _write(job)
            raise ValueError(f"Could not start the job worker: {e}") from None
        with _children_lock:
            _children.append(proc)
        job["pid"] = proc.pid
        _write(job)
    return job


def list_jobs(limit: int | None = 20) -> list[dict]:
    """Recent jobs, newest first. limit=None returns all of them."""
    reap()
    if limit is not None:
        limit = max(1, min(int(limit), 200))
    if not JOBS_DIR.exists():
        return []
    found = []
    for d in JOBS_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            job = json.loads((d / "job.json").read_text())
            job["created"]  # noqa: B018 (must exist)
            found.append(refresh(job))
        except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
            continue
    found.sort(key=lambda j: j["created"], reverse=True)
    return found if limit is None else found[:limit]


def active_jobs() -> list[dict]:
    return [j for j in list_jobs(None) if j["status"] in ACTIVE]


def result(job_id: str) -> tuple[dict, str | None]:
    reap()
    job = refresh(load(job_id))
    f = job_dir(job_id) / "result.md"
    return job, (f.read_text() if f.exists() else None)


def cancel(job_id: str, reason: str = "cancelled") -> dict:
    reap()
    with _lock(job_id):
        job = _read(job_id)
        if job["status"] not in ACTIVE:
            return job
        if worker_alive(job_id) and job.get("pid"):
            # The worker is verifiably alive, so this pid is really ours.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(job["pid"], signal.SIGTERM)
        job.update(status="cancelled", finished=now(), error=reason if reason != "cancelled" else None)
        _write(job)
        return job


def cancel_all(reason: str) -> list[dict]:
    return [cancel(j["id"], reason) for j in active_jobs()]


def summary_line(job: dict) -> str:
    where = f" path={job['path']}" if job.get("path") else ""
    task = job["task"].replace("\n", " ")
    task = task[:80] + ("..." if len(task) > 80 else "")
    return f"{job['id']}  {job['status']:9} {job['role']:10} {job['model']}{where}  {task}"
