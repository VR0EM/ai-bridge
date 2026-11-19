"""Worker process for one background job: python -m ai_bridge.worker <job_id>.

1. Hold alive.lock for its whole life (that is how others know it is alive).
2. Wait for a free slot (max_parallel_jobs); give up if the job is cancelled
   or the bridge is switched off meanwhile.
3. Run agy in the cage (see sandbox.py), in its own process group.
4. Save result.md (also when agy reports an error after a full answer),
   update job.json and post to the shared log.
Any unexpected error marks the job as failed instead of leaving it hanging.
"""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time

from . import config, gemini, jobs, sandbox, store

_agy: subprocess.Popen | None = None


def _on_sigterm(_signum, _frame):
    if _agy is not None and _agy.poll() is None:
        gemini.kill_group(_agy.pid)
    sys.exit(143)


def hold_alive_lock(job_id: str):
    f = open(jobs.job_dir(job_id) / "alive.lock", "a")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f  # keep open: the lock lives as long as this process


def should_stop(job_id: str) -> str | None:
    if not store.is_enabled():
        return "Bridge was switched off."
    if jobs.load(job_id)["status"] == "cancelled":
        return "cancelled"
    return None


def acquire_slot(job_id: str, n: int):
    store.ensure_dir(jobs.SLOTS_DIR)
    while True:
        stop = should_stop(job_id)
        if stop:
            return None, stop
        for i in range(n):
            f = open(jobs.SLOTS_DIR / f"slot-{i}.lock", "a")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f, None
            except BlockingIOError:
                f.close()
        time.sleep(3)


def log_note(job: dict, text: str) -> None:
    try:
        store.append("gemini", f"[job {job['id']} {job['role']}] {text}"[: store.MAX_TEXT], job.get("thread"))
    except ValueError:
        pass


def fail(job_id: str, error: str) -> int:
    job = jobs.update(job_id, only_if_active=True, status="failed", finished=jobs.now(), error=error)
    if job["status"] == "failed":
        log_note(job, f"failed: {error[:300]}")
    return 1


def run(job_id: str) -> int:
    global _agy
    job = jobs.update(job_id, worker_started=True, pid=os.getpid())
    cfg = config.load()

    slot, stop = acquire_slot(job_id, cfg["max_parallel_jobs"])  # noqa: F841 (held until exit)
    if stop:
        jobs.update(job_id, only_if_active=True, status="cancelled", finished=jobs.now(),
                    error=None if stop == "cancelled" else stop)
        return 0

    job = jobs.update(job_id, only_if_active=True, status="running", started=jobs.now())
    if job["status"] != "running":
        return 0

    d = jobs.job_dir(job_id)
    settings_file = sandbox.write_settings(
        d / "settings.json",
        sandbox.agy_settings(cfg["deny_commands"], read_only=True, web=job["role"] == "researcher"),
    )
    timeout_s = int(job["timeout_min"]) * 60
    args = gemini.build_args(
        jobs.ROLE_PROMPTS[job["role"]] + job["task"],
        model=job["model"], timeout_s=timeout_s, settings_file=settings_file,
    )

    _agy = subprocess.Popen(args, cwd=job["cwd"], env=gemini.child_env(int(job["depth"])),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = _agy.communicate(timeout=timeout_s + 60)
    except subprocess.TimeoutExpired:
        gemini.kill_group(_agy.pid)
        out, err = _agy.communicate()
        store.atomic_write(d / "agy-stdout.jsonl", out or "")
        store.atomic_write(d / "agy-stderr.log", err or "")
        return fail(job_id, f"Timed out after {job['timeout_min']} minutes.")
    store.atomic_write(d / "agy-stdout.jsonl", out or "")
    store.atomic_write(d / "agy-stderr.log", err or "")

    try:
        parsed = gemini.parse_stream(out or "")
        _, warning = gemini.outcome(parsed)
    except gemini.GeminiError as e:
        return fail(job_id, f"{e} {(err or '').strip()[-400:]}".strip())

    store.atomic_write(d / "result.md", parsed["response"])
    job = jobs.update(job_id, only_if_active=True, status="done", finished=jobs.now(),
                      usage=parsed["usage"], warning=warning,
                      conversation_id=parsed["conversation_id"])
    if job["status"] == "done":
        first = parsed["response"].strip().splitlines()
        log_note(job, "done" + (" (with warning)" if warning else "") + ". "
                 + (first[0][:300] if first else ""))
    return 0


def main(job_id: str) -> int:
    store.ensure_home()  # sets umask 077: every file this worker writes is private
    signal.signal(signal.SIGTERM, _on_sigterm)
    # Hold alive.lock across run() and fail(), so nobody declares us dead
    # while we are still recording what went wrong.
    alive = hold_alive_lock(job_id)
    try:
        return run(job_id)
    except SystemExit:
        raise
    except Exception as e:  # never leave a job stuck in queued/running
        try:
            return fail(job_id, f"Worker crashed: {type(e).__name__}: {e}")
        except Exception:
            return 1
    finally:
        alive.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
