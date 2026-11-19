"""Worker process for one background job: python -m ai_bridge.worker <job_id>.

1. Wait for a free slot (max_parallel_jobs).
2. Run agy in the tested cage: plan mode, terminal sandbox, black-hole proxy,
   deny list, no SSH agent.
3. Write result.md, update job.json and post to the shared log.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time

from . import config, gemini, jobs, sandbox, store


def acquire_slot(n: int):
    jobs.SLOTS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    while True:
        for i in range(n):
            f = open(jobs.SLOTS_DIR / f"slot-{i}.lock", "a")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f  # keep the file open: the lock lives as long as this process
            except BlockingIOError:
                f.close()
        time.sleep(3)


def job_settings(job: dict, cfg: dict) -> dict:
    deny = [f"command({c})" for c in cfg["deny_commands"]]
    allow = ["read_url(*)"] if job["role"] == "researcher" else []
    return {
        "enableTerminalSandbox": True,
        "toolPermission": "proceed-in-sandbox",
        **sandbox.no_network_settings(),
        "permissions": {"allow": allow, "deny": deny},
    }


def log_note(job: dict, text: str) -> None:
    try:
        store.append("gemini", f"[job {job['id']} {job['role']}] {text}"[: store.MAX_TEXT], job.get("thread"))
    except ValueError:
        pass


def main(job_id: str) -> int:
    job = jobs.load(job_id)
    cfg = config.load()
    slot = acquire_slot(int(cfg["max_parallel_jobs"]))  # noqa: F841 (held until exit)

    job = jobs.load(job_id)
    if job["status"] == "cancelled":
        return 0
    job = jobs.update(job_id, status="running", started=jobs.now())

    d = jobs.job_dir(job_id)
    settings_file = d / "settings.json"
    settings_file.write_text(json.dumps(job_settings(job, cfg), indent=2))
    timeout_s = int(job["timeout_min"]) * 60
    args = [
        gemini.agy_path(),
        "--output-format", "stream-json",
        "--mode", "plan",
        "--sandbox",
        "--settings", str(settings_file),
        "--model", job["model"],
        "--print-timeout", f"{timeout_s}s",
        "-p", jobs.ROLE_PROMPTS[job["role"]] + job["task"],
    ]
    if gemini.FORBIDDEN_FLAGS & set(args):
        jobs.update(job_id, status="failed", finished=jobs.now(), error="Forbidden flag.")
        return 1

    env = gemini.child_env()
    env["AI_BRIDGE_DEPTH"] = str(int(job["depth"]) + 1)
    try:
        proc = subprocess.run(args, cwd=job["cwd"], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout_s + 60)
        (d / "agy-stdout.jsonl").write_text(proc.stdout)
        (d / "agy-stderr.log").write_text(proc.stderr)
        parsed = gemini.parse_stream(proc.stdout)
    except subprocess.TimeoutExpired:
        jobs.update(job_id, status="failed", finished=jobs.now(),
                    error=f"Timed out after {job['timeout_min']} minutes.")
        log_note(job, "failed: timed out")
        return 1
    except gemini.GeminiError as e:
        tail = (proc.stderr or "").strip()[-500:]
        jobs.update(job_id, status="failed", finished=jobs.now(), error=f"{e} {tail}".strip())
        log_note(job, f"failed: {e}")
        return 1

    if jobs.load(job_id)["status"] == "cancelled":
        return 0
    if parsed["status"] != "SUCCESS":
        jobs.update(job_id, status="failed", finished=jobs.now(),
                    error=f"agy status {parsed['status']}", usage=parsed["usage"])
        log_note(job, f"failed: agy status {parsed['status']}")
        return 1

    (d / "result.md").write_text(parsed["response"] or "(empty answer)")
    jobs.update(job_id, status="done", finished=jobs.now(), usage=parsed["usage"],
                conversation_id=parsed["conversation_id"])
    first = (parsed["response"] or "").strip().splitlines()
    log_note(job, "done. " + (first[0][:300] if first else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
