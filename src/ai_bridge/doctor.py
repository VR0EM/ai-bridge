"""`bridge doctor`: check that everything is in place, and say how to fix what is not.

Every check returns (level, title, detail, fix): level is ok, warn or fail.
`--deep` also does two tiny real calls to Gemini (one in a folder agy does not
trust) and one to Claude, to prove logins and the sandbox settings work.
`--fix` repairs what belongs to the bridge itself (registrations, permissions,
the sandbox certificate); it never changes your own agy/Claude settings
except the bridge entries.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from . import config, gemini, installer, jobs, sandbox, store

OK, WARN, FAIL = "ok", "warn", "fail"
BROAD_RULE = re.compile(r"\A(command|read_file|write_file|mcp|execute_url|unsandboxed)\(\*\)\Z")


def _version(tool: str | None) -> str | None:
    if not tool:
        return None
    try:
        r = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=20)
        return (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return None


def _rules(obj) -> list[str]:
    """Find permission-rule strings like command(git) anywhere in a JSON structure."""
    found = []
    if isinstance(obj, dict):
        for v in obj.values():
            found += _rules(v)
    elif isinstance(obj, list):
        for v in obj:
            found += _rules(v)
    elif isinstance(obj, str) and re.match(r"\A[a-z_]+\(.*\)\Z", obj):
        found.append(obj)
    return found


def checks() -> list[tuple[str, str, str, str]]:
    out = []
    add = lambda *c: out.append(c)  # noqa: E731

    add(OK if store.is_enabled() else WARN, "Bridge switch",
        "ON" if store.is_enabled() else "OFF: agents cannot use the bridge", "bridge on")

    if store.HOME.exists():
        mode = stat.S_IMODE(store.HOME.stat().st_mode)
        add(OK if mode == 0o700 else FAIL, "Data folder", f"{store.HOME} (mode {oct(mode)})",
            "bridge doctor --fix")
    else:
        add(WARN, "Data folder", f"{store.HOME} does not exist yet", "bridge install")

    try:
        cfg = config.load()
        add(OK, "Settings", f"model {cfg['gemini_model']}, {cfg['max_parallel_jobs']} parallel jobs", "")
    except ValueError as e:
        add(FAIL, "Settings", str(e), f"fix or delete {config.CONFIG_FILE}")

    try:
        srv = installer.server_path()
        add(OK, "Bridge server", srv, "")
    except RuntimeError as e:
        srv = None
        add(FAIL, "Bridge server", str(e), "./install.sh")

    agy = installer.find("agy")
    v = _version(agy)
    add(OK if v else FAIL, "Antigravity CLI (agy)", v or "not found",
        "curl -fsSL https://antigravity.google/cli/install.sh | bash, then run: agy")
    claude = installer.find("claude")
    v = _version(claude)
    add(OK if v else WARN, "Claude Code (claude)", v or "not found (needed for claude_ask and Claude Code use)",
        "https://code.claude.com/docs/en/setup")

    # Registrations
    cc = installer.read_json(installer.claude_code_file()).get("mcpServers", {}).get(installer.NAME)
    _reg(add, "Claude Code registration", cc, "claude", srv, claude is not None)
    desktop_cfg = installer.claude_desktop_config()
    if desktop_cfg.parent.exists():
        entry = installer.read_json(desktop_cfg).get("mcpServers", {}).get(installer.NAME)
        _reg(add, "Claude desktop registration", entry, "claude", srv, True)
    try:
        entry = installer.read_json(installer.agy_mcp_file()).get("mcpServers", {}).get(installer.NAME)
    except (ValueError, json.JSONDecodeError):
        entry = None
    _reg(add, "Antigravity registration", entry, "gemini", srv, agy is not None)

    agy_settings = {}
    try:
        agy_settings = installer.read_json(installer.agy_settings_file())
    except (ValueError, json.JSONDecodeError):
        add(FAIL, "agy settings", f"{installer.agy_settings_file()} is not valid JSON", "fix the file by hand")
    if agy:
        allowed = installer.ALLOW_RULE in agy_settings.get("permissions", {}).get("allow", [])
        add(OK if allowed else FAIL, "agy may use the bridge tools",
            installer.ALLOW_RULE + (" present" if allowed else " missing"), "bridge doctor --fix")

    trusted = agy_settings.get("trustedWorkspaces", [])
    home = str(Path.home())
    broad = [t for t in trusted if t in (home, home + "/", "/")]
    if broad:
        add(WARN, "agy trusted folders", f"your whole home folder is trusted: {broad}",
            "remove it from trustedWorkspaces in ~/.gemini/antigravity-cli/settings.json "
            "(run 'bridge doctor --deep' first to confirm headless runs still work untrusted)")
    elif agy:
        add(OK, "agy trusted folders", f"{len(trusted)} specific folder(s)", "")

    shared = home_path(".gemini", "config", "config.json")
    if shared.exists():
        try:
            rules = _rules(json.loads(shared.read_text()))
            wide = [r for r in rules if BROAD_RULE.match(r)]
            add(WARN if wide else OK, "Antigravity shared permissions",
                f"{len(rules)} rule(s)" + (f"; very broad: {', '.join(wide)}" if wide else ""),
                "review them in the Antigravity app settings" if wide else "")
        except (OSError, json.JSONDecodeError):
            add(WARN, "Antigravity shared permissions", f"could not read {shared}", "")

    add(OK if sandbox.CA_FILE.exists() else WARN, "Sandbox network lock",
        "black-hole proxy certificate present" if sandbox.CA_FILE.exists() else "certificate missing",
        "bridge doctor --fix")

    active = jobs.active_jobs() if jobs.JOBS_DIR.exists() else []
    add(OK, "Jobs", f"{len(active)} queued or running", "")
    return out


def home_path(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def _reg(add, title, entry, agent, srv, applicable):
    if not applicable:
        add(WARN, title, "tool not installed", "")
        return
    if not entry:
        add(FAIL, title, "not registered", "bridge install")
        return
    env_ok = (entry.get("env") or {}).get("AI_BRIDGE_AGENT") == agent
    cmd = entry.get("command")
    cmd_ok = bool(cmd) and Path(cmd).exists() and (srv is None or Path(cmd).resolve() == Path(srv).resolve())
    if env_ok and cmd_ok:
        add(OK, title, f"as {agent}", "")
    else:
        why = [] if env_ok else [f"AI_BRIDGE_AGENT should be {agent}"]
        why += [] if cmd_ok else [f"command points to {cmd}"]
        add(FAIL, title, "; ".join(why), "bridge install")


def deep_checks() -> list[tuple[str, str, str, str]]:
    """Tiny real calls: Gemini in a trusted and an untrusted folder, and Claude."""
    out = []
    agy = installer.find("agy")
    if agy:
        cfg = config.load()
        for label, extra in [("Gemini answers", {}), ("Gemini answers in an untrusted folder",
                                                       {"trustedWorkspaces": []})]:
            with tempfile.TemporaryDirectory(prefix="ai-bridge-doctor-") as tmp:
                settings = {**sandbox.agy_settings(cfg["deny_commands"], read_only=True, web=False), **extra}
                sf = sandbox.write_settings(Path(tmp) / "settings.json", settings)
                work = Path(tmp) / "work"
                work.mkdir()
                args = gemini.build_args("Reply with exactly: OK", model=cfg["gemini_model"],
                                         timeout_s=90, settings_file=sf)
                try:
                    r = subprocess.run(args, cwd=work, env=gemini.child_env(), capture_output=True,
                                       text=True, timeout=150)
                    p = gemini.parse_stream(r.stdout)
                    good = "OK" in p["response"]
                    out.append((OK if good else FAIL, label,
                                "answered" if good else f"status {p['status']}: {p.get('error')}",
                                "" if good else "run: agy  (to log in), then try again"))
                except (gemini.GeminiError, subprocess.TimeoutExpired, OSError) as e:
                    out.append((FAIL, label, str(e)[:200], "run: agy  (to log in), then try again"))
    claude = installer.find("claude")
    if claude:
        try:
            from . import claude as claude_mod
            args = claude_mod.build_args("Reply with exactly: OK", model="", max_turns=1,
                                         read_only_folder=False, session_id=None)
            with tempfile.TemporaryDirectory() as tmp:
                env = gemini.child_env()
                r = subprocess.run(args, cwd=tmp, env=env, capture_output=True, text=True, timeout=120)
            p = claude_mod.parse(r.stdout)
            good = "OK" in p["response"]
            out.append((OK if good else FAIL, "Claude answers (claude_ask)",
                        "answered" if good else p["response"][:200], "" if good else "run: claude  (to log in)"))
        except Exception as e:  # report, never crash the doctor
            out.append((FAIL, "Claude answers (claude_ask)", str(e)[:200], "run: claude  (to log in)"))
    return out


def fix() -> list[str]:
    done = []
    store.ensure_home()
    os.chmod(store.HOME, 0o700)
    done.append("data folder permissions set to 700")
    sandbox.ensure_dummy_ca()
    done.append("sandbox certificate present")
    done += installer.install()
    return done


def report(results) -> int:
    marks = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}
    worst = 0
    for level, title, detail, hint in results:
        print(f"{marks[level]} {title}: {detail}")
        if level != OK and hint:
            print(f"       fix: {hint}")
        worst = max(worst, {OK: 0, WARN: 1, FAIL: 2}[level])
    print()
    print({0: "Everything looks good.", 1: "Working, with warnings.", 2: "Something needs fixing (see FAIL)."}[worst])
    return 1 if worst == 2 else 0
