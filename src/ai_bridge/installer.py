"""`bridge install` / `bridge uninstall`: register the bridge everywhere it is used.

- Claude Code:        claude mcp add-json ai-bridge ... --scope user   (agent: claude)
- Claude desktop app: claude_desktop_config.json, key mcpServers.ai-bridge (agent: claude)
- Antigravity:        agy mcp add ai-bridge ...  (agent: gemini; app, IDE and CLI share it)
                      plus the allow rule mcp(ai-bridge/*) in agy's settings.json

Every file we edit outside ~/.ai-bridge is backed up first, edited as JSON
(other keys are kept as they are) and replaced atomically with its original mode.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from . import sandbox, store

NAME = "ai-bridge"
ALLOW_RULE = "mcp(ai-bridge/*)"


def home() -> Path:
    return Path.home()


def claude_desktop_config() -> Path:
    if sys.platform == "darwin":
        return home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return home() / ".config" / "Claude" / "claude_desktop_config.json"


def agy_settings_file() -> Path:
    return home() / ".gemini" / "antigravity-cli" / "settings.json"


def agy_mcp_file() -> Path:
    return home() / ".gemini" / "config" / "mcp_config.json"


def claude_code_file() -> Path:
    return home() / ".claude.json"


def server_path() -> str:
    found = shutil.which("ai-bridge-server")
    if found:
        return found
    fallback = home() / ".local" / "bin" / "ai-bridge-server"
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("ai-bridge-server not found. Run: uv tool install --editable <repo>")


def find(tool: str) -> str | None:
    return shutil.which(tool) or (str(p) if (p := home() / ".local" / "bin" / tool).exists() else None)


def read_json(path: Path) -> dict:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object.")
    return data


def edit_json(path: Path, change) -> bool:
    """Back up, apply change(data) -> data, write atomically. Returns True if something changed."""
    data = read_json(path)
    before = json.dumps(data, sort_keys=True)
    data = change(data)
    if json.dumps(data, sort_keys=True) == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if path.exists():
        mode = path.stat().st_mode & 0o777
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_name(f"{path.name}.backup-{stamp}"))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)


def server_entry(agent: str) -> dict:
    return {"command": server_path(), "args": [], "env": {"AI_BRIDGE_AGENT": agent}}


def install() -> list[str]:
    """Register everywhere possible. Returns human-readable report lines."""
    report = []
    store.ensure_home()
    sandbox.ensure_dummy_ca()
    srv = server_path()

    claude = find("claude")
    if claude:
        run([claude, "mcp", "remove", NAME, "--scope", "user"])
        cfg = json.dumps({"type": "stdio", **server_entry("claude")})
        r = run([claude, "mcp", "add-json", NAME, cfg, "--scope", "user"])
        report.append(("ok" if r.returncode == 0 else "FAILED") + " Claude Code"
                      + ("" if r.returncode == 0 else f": {r.stderr.strip()[-200:]}"))
    else:
        report.append("skip Claude Code (claude not installed)")

    desktop = claude_desktop_config()
    if desktop.parent.exists():
        def add_desktop(d):
            d.setdefault("mcpServers", {})[NAME] = server_entry("claude")
            return d
        edit_json(desktop, add_desktop)
        report.append("ok Claude desktop app (restart it to load the bridge)")
    else:
        report.append("skip Claude desktop app (not installed)")

    agy = find("agy")
    if agy:
        r = run([agy, "mcp", "add", "--env", "AI_BRIDGE_AGENT=gemini", NAME, srv])
        report.append(("ok" if r.returncode == 0 else "FAILED") + " Antigravity (agy, app, IDE)"
                      + ("" if r.returncode == 0 else f": {r.stderr.strip()[-200:]}"))

        def add_allow(d):
            allow = d.setdefault("permissions", {}).setdefault("allow", [])
            if ALLOW_RULE not in allow:
                allow.append(ALLOW_RULE)
            return d
        edit_json(agy_settings_file(), add_allow)
        report.append(f"ok Antigravity permission {ALLOW_RULE}")
    else:
        report.append("skip Antigravity (agy not installed: https://antigravity.google/docs/cli/install)")

    if not store.is_enabled():
        store.set_enabled(True)
        report.append("ok bridge switched ON")
    return report


def uninstall(purge: bool = False) -> list[str]:
    report = []
    claude = find("claude")
    if claude:
        r = run([claude, "mcp", "remove", NAME, "--scope", "user"])
        report.append("ok removed from Claude Code" if r.returncode == 0 else "skip Claude Code (not registered)")

    desktop = claude_desktop_config()
    if desktop.exists():
        def drop(d):
            d.get("mcpServers", {}).pop(NAME, None)
            return d
        changed = edit_json(desktop, drop)
        report.append("ok removed from Claude desktop app" if changed else "skip Claude desktop app (not registered)")

    agy = find("agy")
    if agy:
        r = run([agy, "mcp", "remove", NAME])
        report.append("ok removed from Antigravity" if r.returncode == 0 else "skip Antigravity (not registered)")
    if agy_settings_file().exists():
        def drop_allow(d):
            allow = d.get("permissions", {}).get("allow", [])
            if ALLOW_RULE in allow:
                allow.remove(ALLOW_RULE)
            return d
        if edit_json(agy_settings_file(), drop_allow):
            report.append(f"ok removed permission {ALLOW_RULE}")

    from . import jobs
    stopped = jobs.cancel_all("Bridge was uninstalled.")
    store.set_enabled(False)
    report.append(f"ok bridge switched OFF ({len(stopped)} job(s) stopped)")
    if purge:
        shutil.rmtree(store.HOME, ignore_errors=True)
        report.append(f"ok deleted all bridge data in {store.HOME}")
    else:
        report.append(f"kept your log and jobs in {store.HOME} (use --purge to delete them)")
    return report
