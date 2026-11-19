import json

import pytest

from ai_bridge import gemini, sandbox


def stream(status="SUCCESS", response="hi", error=None):
    return "\n".join([
        json.dumps({"event": "init", "conversation_id": "abcdef12-3456"}),
        json.dumps({"event": "result", "result": {"conversation_id": "abcdef12-3456",
                    "status": status, "response": response, "error": error, "usage": {}}}),
    ])


def test_parse_and_outcome():
    p = gemini.parse_stream(stream())
    assert p["conversation_id"] == "abcdef12-3456"
    assert gemini.outcome(p) == ("ok", None)


def test_answer_is_kept_when_agy_reports_error_afterwards():
    kind, warning = gemini.outcome(gemini.parse_stream(stream("ERROR", "full answer", "stream interrupted")))
    assert kind == "partial"
    assert "stream interrupted" in warning


def test_error_without_answer_raises():
    with pytest.raises(gemini.GeminiError):
        gemini.outcome(gemini.parse_stream(stream("ERROR", "", "API error")))
    with pytest.raises(gemini.GeminiError):
        gemini.parse_stream("garbage")


def test_build_args_cage_and_prompt_last(fake_agy, tmp_path):
    args = gemini.build_args("-rm everything", model="gemini-4-neon-high", timeout_s=60,
                             settings_file=tmp_path / "s.json")
    assert "--sandbox" in args and "--settings" in args
    assert args[-2] == "-p" and not args[-1].startswith("-")
    assert not any(a.startswith("--dangerously") for a in args)


@pytest.mark.parametrize("model", ["claude-opus-5-5-high", "gemini-4 --x", "", "gemini-"])
def test_bad_models_rejected(model):
    with pytest.raises(ValueError):
        gemini.check_model(model)


def test_child_env_is_an_allowlist(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("AI_BRIDGE_AGENT", "claude")
    env = gemini.child_env()
    assert "GITHUB_TOKEN" not in env and "SSH_AUTH_SOCK" not in env
    assert env["AI_BRIDGE_AGENT"] == "gemini"
    assert env["AI_BRIDGE_DEPTH"] == "1"
    assert "PATH" in env


def test_conversation_ids_are_validated():
    gemini.save_conversation("t", "abcdef12-3456")
    assert gemini.get_conversation("t") == "abcdef12-3456"
    gemini.save_conversation("t", "--evil")
    assert gemini.get_conversation("t") is None


def test_read_only_settings_deny_all_commands():
    s = sandbox.agy_settings(["ssh"], read_only=True, web=False)
    assert "command(*)" in s["permissions"]["deny"]
    assert "command(ssh)" in s["permissions"]["deny"]
    assert s["permissions"]["allow"] == []
    assert s["sandboxProxy"]["http"]["address"] == sandbox.BLACKHOLE_ADDRESS


def test_ask_end_to_end_with_fake_agy(fake_agy, monkeypatch):
    import asyncio
    from ai_bridge import store
    log = store.HOME / "fake_agy.log"
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    r1 = asyncio.run(gemini.ask("first", "t", "gemini-4-neon-high", 30, ["ssh"]))
    r2 = asyncio.run(gemini.ask("second", "t", "gemini-4-neon-high", 30, ["ssh"]))
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert "--conversation" not in calls[0]["args"]
    assert calls[1]["args"][calls[1]["args"].index("--conversation") + 1] == r1["conversation_id"]
    assert "GITHUB_TOKEN" not in calls[0]["env"]
    assert r2["warning"] is None


def test_cancelled_ask_kills_agy(fake_agy):
    import asyncio
    import subprocess
    import time
    t = time.time()
    with pytest.raises((gemini.GeminiError, TimeoutError)):
        asyncio.run(asyncio.wait_for(gemini.ask("SLEEP60 marker-xyz", "t", "gemini-4-neon-high", 1, []), 3))
    assert time.time() - t < 10
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "marker-xyz"], capture_output=True, text=True).stdout
    assert left.strip() == ""
