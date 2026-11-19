"""Test setup: every test runs against a throwaway AI_BRIDGE_HOME and a fake agy."""
import atexit
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

# Must happen before ai_bridge is imported: module paths are resolved at import time.
_HOME = Path(tempfile.mkdtemp(prefix="ai-bridge-test-"))
os.environ["AI_BRIDGE_HOME"] = str(_HOME)
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ.pop("AI_BRIDGE_DEPTH", None)

import pytest  # noqa: E402

FAKE_AGY = Path(__file__).parent / "fake_agy.py"
FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture(autouse=True)
def clean_home():
    for child in _HOME.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink()
    yield _HOME


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    """Put a fake `agy` first on PATH (behaviour is driven by words in the prompt)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = bin_dir / "agy"
    target.write_text(f"#!{sys.executable}\n" + FAKE_AGY.read_text())
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return target


@pytest.fixture
def home_dir():
    """A real folder inside the user's home (the reviewer only accepts those)."""
    d = Path(tempfile.mkdtemp(prefix="ai-bridge-review-", dir=Path.home()))
    (d / "main.py").write_text("print('hello')\n")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Put a fake `claude` first on PATH."""
    bin_dir = tmp_path / "cbin"
    bin_dir.mkdir()
    target = bin_dir / "claude"
    target.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE.read_text())
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return target
