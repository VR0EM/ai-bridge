import json
import time
from pathlib import Path

import pytest

from ai_bridge import config, jobs, store


def start(task, role="researcher", path=None):
    return jobs.create(role, task, path, "gemini-4-neon-high", "t", "claude", 0, 1)


def wait(job_id, statuses=("done", "failed", "cancelled"), timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        job = jobs.refresh(jobs.load(job_id))
        if job["status"] in statuses:
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} stuck in {jobs.load(job_id)['status']}")


@pytest.fixture(autouse=True)
def bridge_on():
    store.set_enabled(True)


def test_job_runs_to_done_with_result(fake_agy):
    job = start("hello")
    done = wait(job["id"])
    assert done["status"] == "done"
    _, text = jobs.result(job["id"])
    assert "VERDICT: APPROVE" in text
    settings = json.loads((jobs.job_dir(job["id"]) / "settings.json").read_text())
    assert "command(*)" in settings["permissions"]["deny"]
    assert settings["permissions"]["allow"] == ["read_url(*)"]


def test_answer_kept_when_agy_errors_after_answering(fake_agy):
    done = wait(start("PARTIAL")["id"])
    assert done["status"] == "done"
    assert "interrupted" in done["warning"]


def test_failures_are_recorded(fake_agy):
    assert wait(start("FAIL")["id"])["status"] == "failed"
    assert wait(start("EMPTYERR")["id"])["status"] == "failed"


def test_queue_respects_parallel_limit(fake_agy):
    config.save({"max_parallel_jobs": 1})
    a = start("SLEEP2 first")
    b = start("SLEEP2 second")
    time.sleep(1.2)
    states = sorted(jobs.load(j["id"])["status"] for j in (a, b))
    assert states == ["queued", "running"]
    assert wait(a["id"])["status"] == "done"
    assert wait(b["id"])["status"] == "done"


def test_cancel_running_job_stops_it(fake_agy):
    job = start("SLEEP30 cancel-me")
    time.sleep(1.5)
    assert jobs.cancel(job["id"])["status"] == "cancelled"
    time.sleep(1)
    assert not jobs.worker_alive(job["id"])
    assert jobs.load(job["id"])["status"] == "cancelled"


def test_bridge_off_stops_queued_and_running_jobs(fake_agy):
    config.save({"max_parallel_jobs": 1})
    a = start("SLEEP30 running")
    b = start("SLEEP30 queued")
    time.sleep(1.5)
    store.set_enabled(False)
    stopped = jobs.cancel_all("Bridge was switched off.")
    assert {j["id"] for j in stopped} == {a["id"], b["id"]}
    time.sleep(1)
    assert not jobs.worker_alive(a["id"]) and not jobs.worker_alive(b["id"])


def test_dead_worker_is_marked_failed():
    store.ensure_dir(jobs.JOBS_DIR / "deadbeefdeadbeef")
    job = {"id": "deadbeefdeadbeef", "role": "researcher", "task": "x", "path": None,
           "model": "gemini-4-neon-high", "status": "running", "worker_started": True,
           "created": jobs.now(), "pid": 999999}
    (jobs.JOBS_DIR / job["id"] / "job.json").write_text(json.dumps(job))
    assert jobs.refresh(jobs.load(job["id"]))["status"] == "failed"


def test_worker_crash_does_not_leave_job_hanging(fake_agy):
    config.CONFIG_FILE.write_text("{broken")  # jobs.create() does not read config; the worker does
    done = wait(start("hello")["id"])
    assert done["status"] == "failed"
    assert "Worker crashed" in done["error"]


def test_review_path_rules(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for name in ["projects/app", ".ssh", "LIBRARY", "work/bridge-data"]:
        (home / name).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert jobs.check_review_path(str(home / "projects/app")) == str((home / "projects/app").resolve())
    for bad, msg in [(None, "needs a 'path'"), (str(home), "inside your home"),
                     ("/etc", "inside your home"), (str(home / ".ssh"), "hidden or system"),
                     (str(home / "LIBRARY"), "hidden or system"),
                     (str(home / "nope"), "Not a folder")]:
        with pytest.raises(ValueError, match=msg):
            jobs.check_review_path(bad)
    # A folder that contains a custom bridge data folder is refused too
    monkeypatch.setattr(store, "HOME", home / "work" / "bridge-data")
    with pytest.raises(ValueError, match="data folder"):
        jobs.check_review_path(str(home / "work"))


def test_reviewer_runs_inside_the_folder(fake_agy, home_dir):
    log = store.HOME / "fake_agy.log"
    done = wait(jobs.create("reviewer", "check", str(home_dir), "gemini-4-neon-high",
                            None, "claude", 0, 1)["id"])
    assert done["status"] == "done"
    call = json.loads(log.read_text().splitlines()[0])
    assert Path(call["cwd"]).resolve() == home_dir.resolve()
    settings = json.loads(Path(call["args"][call["args"].index("--settings") + 1]).read_text())
    assert settings["permissions"]["allow"] == []
    assert "read_url(*)" in settings["permissions"]["deny"]


@pytest.mark.parametrize("bad", ["../../etc", "", "XYZ", "a" * 40])
def test_job_ids_are_validated(bad):
    with pytest.raises(ValueError):
        jobs.load(bad)
