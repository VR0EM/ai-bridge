import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from ai_bridge import claude, config, store


@pytest.fixture(autouse=True)
def bridge_on():
    store.set_enabled(True)


def calls():
    return [json.loads(line) for line in (store.HOME / "fake_claude.log").read_text().splitlines()]


def test_claude_is_boxed_in(fake_claude, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    r = asyncio.run(claude.ask("-v review my plan", "t", None, "", 30))
    assert r["response"] == "Claude says: looks fine."
    args = calls()[0]["args"]
    for flag in ["--safe-mode", "--strict-mcp-config", "-p"]:
        assert flag in args
    assert args[args.index("--mcp-config") + 1] == json.dumps({"mcpServers": {}})
    assert args[args.index("--permission-mode") + 1] == "plan"
    assert args[args.index("--tools") + 1] == ""
    assert args[-2:] == ["--", "-v review my plan"]  # the question can never be a flag
    assert not any("dangerously" in a or "bypass" in a for a in args)
    env = calls()[0]["env"]
    assert "GITHUB_TOKEN" not in env and env["AI_BRIDGE_AGENT"] == "claude"


def test_conversation_continues_per_thread(fake_claude):
    first = asyncio.run(claude.ask("one", "t", None, "", 30))
    asyncio.run(claude.ask("two", "t", None, "", 30))
    asyncio.run(claude.ask("three", "other", None, "", 30))
    c = calls()
    assert "--resume" not in c[0]["args"]
    assert c[1]["args"][c[1]["args"].index("--resume") + 1] == first["session_id"]
    assert "--resume" not in c[2]["args"]


def test_folder_access_is_read_only_and_checked(fake_claude, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    (home / ".ssh").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    asyncio.run(claude.ask("look", "t", str(home / "proj"), "opus", 30))
    c = calls()[0]
    assert Path(c["cwd"]).resolve() == (home / "proj").resolve()
    assert c["args"][c["args"].index("--tools") + 1] == "Read,Glob,Grep"
    assert c["args"][c["args"].index("--model") + 1] == "opus"
    with pytest.raises(ValueError, match="hidden or system"):
        asyncio.run(claude.ask("look", "t", str(home / ".ssh"), "", 30))


def test_failure_and_kill_switch(fake_claude):
    with pytest.raises(claude.ClaudeError):
        asyncio.run(claude.ask("CFAIL", "t", None, "", 30))
    threading.Timer(1.0, store.set_enabled, args=(False,)).start()
    t = time.time()
    with pytest.raises(store.BridgeOff):
        asyncio.run(claude.ask("SLEEP60", "t", None, "", 120))
    assert time.time() - t < 8


def test_rate_limit():
    for _ in range(3):
        store.append("gemini", f"{claude.QUESTION_PREFIX} hi")
    claude.check_rate(4)
    with pytest.raises(ValueError, match="limit"):
        claude.check_rate(3)


def test_claude_model_config():
    config.save({"claude_model": "sonnet"})
    with pytest.raises(ValueError):
        config.save({"claude_model": "--evil"})
