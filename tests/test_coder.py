import json
import subprocess
import time
from pathlib import Path

import pytest

from ai_bridge import config, jobs, store


def sh(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    r = home / "projects" / "app"
    r.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (r / "main.py").write_text("print('hello')\n")
    (r / ".gitignore").write_text("*.log\n")
    sh("init", "-q", "-b", "main", cwd=r)
    sh("add", "-A", cwd=r)
    sh("commit", "-qm", "initial", cwd=r)
    return r


@pytest.fixture(autouse=True)
def bridge_on():
    store.set_enabled(True)


def start(repo, task):
    return jobs.create("coder", task, str(repo), "gemini-4-neon-high", None, "claude", 0, 1)


def wait(job_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        job = jobs.refresh(jobs.load(job_id))
        if job["status"] not in jobs.ACTIVE:
            return job
        time.sleep(0.2)
    raise AssertionError("job stuck")


def test_coder_changes_end_up_on_a_branch_only_after_approval(fake_agy, repo):
    head_before = sh("rev-parse", "HEAD", cwd=repo).strip()
    job = start(repo, "EDIT please add a line")
    done = wait(job["id"])
    assert done["status"] == "done", done
    info = done["coder"]
    assert info["changed"]
    # Nothing touched the real repo yet
    assert sh("rev-parse", "HEAD", cwd=repo).strip() == head_before
    assert sh("status", "--porcelain", cwd=repo) == ""
    _, patch = jobs.diff(job["id"])
    assert "edited by gemini" in patch and "new_file.txt" in patch
    assert ".git/" not in patch  # Gemini's planted .git folder is never content
    assert "__pycache__" not in patch  # build junk is excluded by default
    approved = jobs.approve(job["id"])
    assert approved["status"] == "approved"
    branch = approved["branch"]
    assert sh("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip() == "main"
    assert "edited by gemini" in sh("show", f"{branch}:main.py", cwd=repo)
    assert sh("rev-parse", f"{branch}^", cwd=repo).strip() == head_before
    assert not (jobs.job_dir(job["id"]) / "workspace").exists()


def test_planted_git_config_hooks_and_filters_never_run(fake_agy, repo):
    done = wait(start(repo, "EDIT")["id"])
    assert done["status"] == "done"
    jobs.approve(done["id"])
    assert not (store.HOME / "PWNED").exists()


def test_risk_flags(fake_agy, repo):
    done = wait(start(repo, "EDIT SYMLINK EXEC BINARY")["id"])
    risks = "\n".join(done["coder"]["risks"])
    assert "symlink: innocent_link -> /etc/passwd" in risks
    assert "made executable: run.sh" in risks
    assert "sensitive file changed: .gitattributes" in risks
    assert "binary file: blob.bin" in risks


def test_coder_settings_allow_commands_but_no_web(fake_agy, repo):
    log = store.HOME / "fake_agy.log"
    wait(start(repo, "EDIT")["id"])
    call = json.loads(log.read_text().splitlines()[0])
    args = call["args"]
    assert args[args.index("--mode") + 1] == "accept-edits"
    settings = json.loads(Path(args[args.index("--settings") + 1]).read_text())
    assert "command(*)" not in settings["permissions"]["deny"]
    assert "read_url(*)" in settings["permissions"]["deny"]
    assert settings["sandboxProxy"]["http"]["address"] == "127.0.0.1:9"


def test_no_changes_and_reject(fake_agy, repo):
    done = wait(start(repo, "do nothing")["id"])
    assert done["status"] == "done" and not done["coder"]["changed"]
    with pytest.raises(ValueError, match="no changes"):
        jobs.approve(done["id"])
    assert jobs.reject(done["id"])["status"] == "rejected"


def test_one_coder_per_repo_and_root_required(fake_agy, repo):
    config.save({"max_parallel_jobs": 2})
    first = start(repo, "SLEEP5 EDIT")
    with pytest.raises(ValueError, match="already coding"):
        start(repo, "EDIT again")
    with pytest.raises(ValueError, match="root folder of a git repository"):
        jobs.create("coder", "x", str(repo.parent), "gemini-4-neon-high", None, "claude", 0, 1)
    jobs.cancel(first["id"])


def test_dirty_repo_is_reported(fake_agy, repo):
    (repo / "main.py").write_text("uncommitted\n")
    job = start(repo, "EDIT")
    assert job["repo"]["dirty"] is True
    wait(job["id"])
