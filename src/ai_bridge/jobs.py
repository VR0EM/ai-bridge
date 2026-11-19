"""Background Gemini jobs (gemini_task).

Each job is a detached worker process (python -m ai_bridge.worker <id>), so it
keeps running when the MCP client restarts. Job state lives in
~/.ai-bridge/jobs/<id>/job.json; the answer in result.md.

Concurrency: a worker must hold one of N slot locks before it starts agy.
Locks are released automatically when a process dies, so a crash never
leaves a slot blocked.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config, store

JOBS_DIR = store.HOME / "jobs"
SLOTS_DIR = store.HOME / "slots"

ROLES = {"researcher", "reviewer"}

# Folders a reviewer may never be pointed at
SENSITIVE = [".ssh", ".gnupg", ".gemini", ".claude", ".ai-bridge", ".aws", ".azure",
             ".kube", ".docker", ".config", ".netrc", "Library"]

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
        "Read what you need. Do not modify any files and do not run shell commands.\n"
        "Deliver a review in Markdown with: a one-paragraph summary, a list of issues "
        "(severity: high/medium/low, file and line where possible, why it matters, how to "
        "fix), and a last line that is exactly 'VERDICT: APPROVE' or "
        "'VERDICT: CHANGES_REQUESTED'.\n\n"
        "Review request:\n"
    ),
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def job_dir(job_id: str) -> Path:
    if not job_id or not all(c in "0123456789abcdef" for c in job_id):
        raise ValueError("Invalid job id.")
    return JOBS_DIR / job_id


def load(job_id: str) -> dict:
    try:
        return json.loads((job_dir(job_id) / "job.json").read_text())
    except FileNotFoundError:
        raise ValueError(f"Unknown job: {job_id}") from None


def save(job: dict) -> None:
    d = job_dir(job["id"])
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, indent=2))
    tmp.replace(d / "job.json")


def update(job_id: str, **fields) -> dict:
    job = load(job_id)
    job.update(fields)
    save(job)
    return job


def is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def refresh(job: dict) -> dict:
    """Mark jobs whose worker died unexpectedly as failed."""
    if job["status"] in {"queued", "running"} and not is_alive(job.get("pid")):
        job = update(job["id"], status="failed", finished=now(),
                     error="Worker process stopped unexpectedly.")
    return job


def check_review_path(path: str | None) -> str:
    if not path:
        raise ValueError("A reviewer needs a 'path' to the folder to review.")
    p = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    if not p.is_dir():
        raise ValueError(f"Not a folder: {p}")
    if p == home or home not in p.parents:
        raise ValueError("The review path must be a folder inside your home folder (not the home folder itself).")
    rel = p.relative_to(home).parts
    if rel and rel[0] in SENSITIVE:
        raise ValueError(f"Refusing to review a sensitive folder: ~/{rel[0]}")
    return str(p)


def create(role: str, task: str, path: str | None, model: str, thread: str | None,
           requested_by: str, depth: int) -> dict:
    if role not in ROLES:
        raise ValueError(f"Unknown role {role!r}. Choose from: {', '.join(sorted(ROLES))}.")
    task = task.strip()
    if not task:
        raise ValueError("Empty task.")
    cwd = check_review_path(path) if role == "reviewer" else None

    store.ensure_home()
    JOBS_DIR.mkdir(mode=0o700, exist_ok=True)
    job_id = secrets.token_hex(4)
    d = job_dir(job_id)
    d.mkdir(mode=0o700)
    workdir = d / "workdir"
    workdir.mkdir(mode=0o700)
    cfg = config.load()
    job = {
        "id": job_id,
        "role": role,
        "task": task,
        "path": cwd,
        "cwd": cwd or str(workdir),
        "model": model,
        "thread": thread,
        "requested_by": requested_by,
        "depth": depth,
        "timeout_min": int(cfg["job_timeout_min"]),
        "status": "queued",
        "created": now(),
        "started": None,
        "finished": None,
        "pid": None,
        "error": None,
        "usage": None,
    }
    save(job)

    log = open(d / "worker.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ai_bridge.worker", job_id],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True,  # own process group: survives client restarts, easy to kill
        env={**os.environ, "AI_BRIDGE_HOME": str(store.HOME)},
    )
    return update(job_id, pid=proc.pid)


def list_jobs(limit: int = 20) -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    jobs = []
    for d in JOBS_DIR.iterdir():
        try:
            jobs.append(refresh(json.loads((d / "job.json").read_text())))
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    jobs.sort(key=lambda j: j["created"], reverse=True)
    return jobs[:limit]


def result(job_id: str) -> tuple[dict, str | None]:
    job = refresh(load(job_id))
    f = job_dir(job_id) / "result.md"
    return job, (f.read_text() if f.exists() else None)


def cancel(job_id: str) -> dict:
    job = refresh(load(job_id))
    if job["status"] not in {"queued", "running"}:
        return job
    try:
        os.killpg(job["pid"], signal.SIGTERM)
    except (ProcessLookupError, PermissionError, TypeError):
        pass
    return update(job_id, status="cancelled", finished=now())


def summary_line(job: dict) -> str:
    where = f" path={job['path']}" if job.get("path") else ""
    task = job["task"].replace("\n", " ")
    task = task[:80] + ("..." if len(task) > 80 else "")
    return f"{job['id']}  {job['status']:9} {job['role']:10} {job['model']}{where}  {task}"
