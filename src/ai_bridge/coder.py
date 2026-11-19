"""Coder jobs: Gemini edits a copy of a repo, Claude reviews the diff.

Why no normal clone: if Gemini's workspace contained a .git folder, it could
plant hooks, filters or core.fsmonitor there, and our own git commands (run
outside the sandbox) would execute them on the host. So:

  jobs/<id>/trusted.git   bare copy of the repo, outside the sandbox: only we touch it
  jobs/<id>/workspace/    plain files of the base commit, no .git: Gemini works here
  jobs/<id>/index         our own index file

After Gemini is done we build one commit ourselves with a hardened git
(no global/system config, no hooks, no fsmonitor, no signing) using
--git-dir=trusted.git --work-tree=workspace. Git never treats a folder named
.git inside the work tree as content, so anything Gemini puts there is ignored.

Approve = fetch that commit into the real repo as branch gemini/<id>.
The user's current branch is never touched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from . import store

SAFE_GIT_CONFIG = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "commit.gpgsign=false",
    "-c", "core.autocrlf=false",
    "-c", "protocol.file.allow=always",
]
AUTHOR = {"GIT_AUTHOR_NAME": "Gemini (ai-bridge)", "GIT_AUTHOR_EMAIL": "ai-bridge@localhost",
          "GIT_COMMITTER_NAME": "ai-bridge", "GIT_COMMITTER_EMAIL": "ai-bridge@localhost"}
MAX_DIFF_CHARS = 60000
RISKY_PATHS = (".github/", ".gitlab-ci", ".gitattributes", ".gitmodules", ".gitignore",
               ".pre-commit-config", ".envrc", ".vscode/", "Makefile", "package.json",
               "pyproject.toml", "setup.py", "Dockerfile", "docker-compose")


class CoderError(ValueError):
    pass


def git_bin() -> str:
    found = shutil.which("git")
    if not found:
        raise CoderError("git not found.")
    return found


def git(*args: str, git_dir: Path | None = None, work_tree: Path | None = None,
        cwd: Path | None = None, index: Path | None = None, extra_env: dict | None = None,
        check: bool = True) -> str:
    """Run git with a hardened, predictable configuration."""
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR"}}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    if index:
        env["GIT_INDEX_FILE"] = str(index)
    env.update(extra_env or {})
    cmd = [git_bin(), *SAFE_GIT_CONFIG]
    if git_dir:
        cmd.append(f"--git-dir={git_dir}")
    if work_tree:
        cmd.append(f"--work-tree={work_tree}")
    proc = subprocess.run([*cmd, *args], cwd=cwd, env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=300)
    if check and proc.returncode != 0:
        raise CoderError(f"git {args[0]} failed: {proc.stderr.strip()[-400:]}")
    return proc.stdout


def check_repo(path: str) -> dict:
    """Validate the source repo (path already passed jobs.check_review_path)."""
    repo = Path(path)
    top = git("rev-parse", "--show-toplevel", cwd=repo, check=False).strip()
    if not top or Path(top).resolve() != repo.resolve():
        raise CoderError("A coder job needs the root folder of a git repository as 'path'.")
    head = git("rev-parse", "--verify", "HEAD^{commit}", cwd=repo, check=False).strip()
    if not head:
        raise CoderError("The repository has no commits yet.")
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip()
    dirty = bool(git("status", "--porcelain", cwd=repo).strip())
    return {"base": head, "branch": branch, "dirty": dirty}


def paths(job_dir: Path) -> tuple[Path, Path, Path]:
    return job_dir / "trusted.git", job_dir / "workspace", job_dir / "index"


def prepare(job_dir: Path, repo: str, base: str) -> Path:
    """Create trusted.git and a .git-free workspace with the files of `base`."""
    trusted, workspace, index = paths(job_dir)
    git("clone", "--quiet", "--bare", "--no-hardlinks", "--no-local", repo, str(trusted))
    git("config", "core.hooksPath", "/dev/null", git_dir=trusted)
    store.ensure_dir(workspace)
    git("read-tree", base, git_dir=trusted, index=index)
    git("checkout-index", "--all", "--force", git_dir=trusted, work_tree=workspace, index=index)
    return workspace


def finalize(job_dir: Path, base: str, job_id: str) -> dict:
    """Turn the workspace into one commit on top of base and describe it."""
    trusted, workspace, index = paths(job_dir)
    # Respects the repo's .gitignore. Attributes in the work tree cannot run
    # filters or diff drivers: those need config, and only our config is used.
    git("add", "--all", "--", ".", git_dir=trusted, work_tree=workspace, index=index)
    tree = git("write-tree", git_dir=trusted, index=index).strip()
    base_tree = git("rev-parse", f"{base}^{{tree}}", git_dir=trusted).strip()
    if tree == base_tree:
        return {"changed": False, "commit": None, "stat": "", "risks": []}
    commit = git("commit-tree", tree, "-p", base, "-m", f"ai-bridge: changes from Gemini job {job_id}",
                 git_dir=trusted, extra_env=AUTHOR).strip()
    git("update-ref", "refs/heads/result", commit, git_dir=trusted)
    stat = git("diff", "--stat", base, commit, git_dir=trusted)
    patch = git("diff", "--binary", base, commit, git_dir=trusted)
    store.atomic_write(job_dir / "changes.patch", patch)
    return {"changed": True, "commit": commit, "stat": stat, "risks": risks(trusted, base, commit)}


def risks(trusted: Path, base: str, commit: str) -> list[str]:
    """Flag changes a reviewer should look at extra carefully."""
    found = []
    raw = git("diff", "--raw", "-z", "--no-renames", base, commit, git_dir=trusted).split("\0")
    i = 0
    while i < len(raw) - 1:
        meta, path = raw[i], raw[i + 1]
        i += 2
        if not meta.startswith(":"):
            continue
        old_mode, new_mode = meta[1:].split()[:2]
        if new_mode == "120000":
            target = git("cat-file", "-p", f"{commit}:{path}", git_dir=trusted, check=False)
            found.append(f"symlink: {path} -> {target.strip()}")
        elif new_mode == "100755" and old_mode != "100755":
            found.append(f"made executable: {path}")
        elif new_mode == "160000":
            found.append(f"submodule: {path}")
        if any(path.startswith(r) or path == r.rstrip("/") for r in RISKY_PATHS):
            found.append(f"sensitive file changed: {path}")
    return found


def diff_text(job_dir: Path) -> str:
    f = job_dir / "changes.patch"
    if not f.exists():
        return ""
    text = f.read_text(errors="replace")
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n... (diff truncated, full patch in {f})"
    return text


def approve(job_dir: Path, repo: str, commit: str, job_id: str) -> str:
    """Bring the commit into the real repo as branch gemini/<id>. Never touches the current branch."""
    trusted, _, _ = paths(job_dir)
    branch = f"gemini/{job_id}"
    if git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=Path(repo), check=False).strip():
        raise CoderError(f"Branch {branch} already exists in the repository.")
    git("fetch", "--quiet", "--no-tags", str(trusted), f"refs/heads/result:refs/heads/{branch}",
        cwd=Path(repo))
    got = git("rev-parse", f"refs/heads/{branch}", cwd=Path(repo)).strip()
    if got != commit:
        raise CoderError("The fetched commit does not match the reviewed commit; aborting.")
    return branch


def cleanup(job_dir: Path) -> None:
    trusted, workspace, index = paths(job_dir)
    for p in (trusted, workspace):
        shutil.rmtree(p, ignore_errors=True)
    index.unlink(missing_ok=True)
