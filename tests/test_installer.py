"""install / uninstall / doctor against a fake home folder and fake agy/claude."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path, fake_agy, fake_claude):
    home = tmp_path / "home"
    home.mkdir()
    desktop = (home / "Library" / "Application Support" / "Claude" if sys.platform == "darwin"
               else home / ".config" / "Claude")
    desktop.mkdir(parents=True)
    (desktop / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {"milo": {"command": "milo"}}}))
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"trustedWorkspaces": [str(home)], "theme": "dark"}))
    os.chmod(settings, 0o600)
    bin_dir = tmp_path / "srvbin"
    bin_dir.mkdir()
    srv = bin_dir / "ai-bridge-server"
    srv.write_text("#!/bin/sh\n")
    srv.chmod(srv.stat().st_mode | stat.S_IEXEC)
    e = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return {"home": home, "desktop": desktop / "claude_desktop_config.json", "settings": settings,
            "srv": str(srv), "env": e}


def bridge(env, *args):
    return subprocess.run([sys.executable, "-m", "ai_bridge.cli", *args],
                          capture_output=True, text=True, env=env["env"], timeout=60)


def test_install_registers_everywhere_and_keeps_other_settings(env):
    r = bridge(env, "install")
    assert r.returncode == 0, r.stderr
    home = env["home"]
    cc = json.loads((home / ".claude.json").read_text())["mcpServers"]["ai-bridge"]
    assert cc["env"]["AI_BRIDGE_AGENT"] == "claude" and cc["command"] == env["srv"]
    desktop = json.loads(env["desktop"].read_text())["mcpServers"]
    assert "milo" in desktop and desktop["ai-bridge"]["env"]["AI_BRIDGE_AGENT"] == "claude"
    agy = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())["mcpServers"]["ai-bridge"]
    assert agy["env"]["AI_BRIDGE_AGENT"] == "gemini"
    settings = json.loads(env["settings"].read_text())
    assert settings["theme"] == "dark" and "mcp(ai-bridge/*)" in settings["permissions"]["allow"]
    assert stat.S_IMODE(env["settings"].stat().st_mode) == 0o600  # original mode kept
    assert list(env["settings"].parent.glob("settings.json.backup-*"))  # backup made
    # Running it twice changes nothing and does not duplicate the rule
    bridge(env, "install")
    assert json.loads(env["settings"].read_text())["permissions"]["allow"].count("mcp(ai-bridge/*)") == 1


def test_doctor_reports_and_fix_repairs(env):
    r = bridge(env, "doctor")
    assert r.returncode == 1 and "[FAIL] Claude Code registration: not registered" in r.stdout
    r = bridge(env, "doctor", "--fix")
    assert "Something needs fixing" not in r.stdout, r.stdout
    assert "[warn] agy trusted folders" in r.stdout  # whole home folder is trusted
    assert "[ ok ] Antigravity registration: as gemini" in r.stdout


def test_doctor_deep_with_fakes(env):
    bridge(env, "install")
    r = bridge(env, "doctor", "--deep")
    assert "[ ok ] Gemini answers: answered" in r.stdout, r.stdout
    assert "[ ok ] Gemini answers in an untrusted folder: answered" in r.stdout
    assert "[ ok ] Claude answers (claude_ask): answered" in r.stdout


def test_uninstall_removes_only_our_entries(env):
    bridge(env, "install")
    r = bridge(env, "uninstall")
    assert r.returncode == 0, r.stderr
    home = env["home"]
    assert "ai-bridge" not in json.loads((home / ".claude.json").read_text())["mcpServers"]
    desktop = json.loads(env["desktop"].read_text())["mcpServers"]
    assert "ai-bridge" not in desktop and "milo" in desktop
    assert "mcp(ai-bridge/*)" not in json.loads(env["settings"].read_text())["permissions"]["allow"]
    assert "kept your log" in r.stdout
