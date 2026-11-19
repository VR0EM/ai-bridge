"""`bridge ui`: a small local settings panel in your browser.

Security:
- Listens on 127.0.0.1 only, on a random free port.
- Every API call needs a random token (sent as the X-Bridge-Token header).
  The token travels in the URL fragment (#token=...), which browsers never
  send to the server, so it does not end up in logs or history requests.
- The Host header must be 127.0.0.1:<port> (blocks DNS-rebinding tricks).
- No CORS headers, and writes require a JSON body plus the token header,
  so other websites cannot drive it.
"""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import config, doctor, installer, jobs, store

_models_cache: list[dict] | None = None
_models_lock = threading.Lock()


def gemini_models() -> list[dict]:
    """Gemini models from `agy models` (cached for the lifetime of the UI)."""
    global _models_cache
    with _models_lock:
        if _models_cache is None:
            found = []
            agy = installer.find("agy")
            if agy:
                try:
                    out = subprocess.run([agy, "models"], capture_output=True, text=True, timeout=30).stdout
                    for line in out.splitlines():
                        m = re.match(r"\s*(gemini-[a-z0-9._-]+)\s+(.*)", line)
                        if m:
                            found.append({"id": m.group(1), "name": m.group(2).strip()})
                except (OSError, subprocess.TimeoutExpired):
                    pass
            _models_cache = found
        return _models_cache


def state() -> dict:
    try:
        cfg, cfg_error = config.load(), None
    except ValueError as e:
        cfg, cfg_error = config._defaults(), str(e)
    return {
        "enabled": store.is_enabled(),
        "data_dir": str(store.HOME),
        "config": cfg,
        "config_error": cfg_error,
        "defaults": config.DEFAULTS,
        "ranges": config.RANGES,
        "models": gemini_models(),
        "jobs": jobs.list_jobs(30) if jobs.JOBS_DIR.exists() else [],
        "checks": [list(c) for c in doctor.checks()],
    }


def make_handler(token: str, port_holder: dict):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ai-bridge-ui"

        def log_message(self, *_args):  # keep the terminal quiet
            pass

        def _send(self, code: int, body, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, need_token: bool) -> bool:
            if self.headers.get("Host") != f"127.0.0.1:{port_holder['port']}":
                self._send(HTTPStatus.FORBIDDEN, {"error": "bad host"})
                return False
            if need_token and not secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), token):
                self._send(HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong token"})
                return False
            return True

        def _body(self) -> dict:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("expected application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000:
                raise ValueError("body too large")
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                if self._guard(False):
                    html = resources.files("ai_bridge").joinpath("ui.html").read_bytes()
                    self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")
                return
            if not self._guard(True):
                return
            try:
                if path == "/api/state":
                    self._send(HTTPStatus.OK, state())
                elif path == "/api/log":
                    since = int((self.path.split("since=") + ["0"])[1].split("&")[0] or 0)
                    entries = [e for e in store.read_all() if e.get("id", 0) > since][-300:]
                    self._send(HTTPStatus.OK, {"entries": entries})
                else:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except Exception as e:  # never crash the panel
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

        def do_POST(self):
            if not self._guard(True):
                return
            path = self.path.split("?")[0]
            try:
                body = self._body()
                if path == "/api/on":
                    store.set_enabled(True)
                    self._send(HTTPStatus.OK, {"ok": True})
                elif path == "/api/off":
                    store.set_enabled(False)
                    stopped = jobs.cancel_all("Bridge was switched off.")
                    self._send(HTTPStatus.OK, {"ok": True, "stopped": len(stopped)})
                elif path == "/api/config":
                    self._send(HTTPStatus.OK, {"config": config.save(body)})
                elif path == "/api/config/reset":
                    store.atomic_write(config.CONFIG_FILE, "{}")
                    self._send(HTTPStatus.OK, {"config": config.load()})
                elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                    job = jobs.cancel(path.split("/")[3])
                    self._send(HTTPStatus.OK, {"job": job})
                elif path == "/api/doctor/deep":
                    self._send(HTTPStatus.OK, {"checks": [list(c) for c in doctor.deep_checks()]})
                else:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except ValueError as e:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except Exception as e:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

    return Handler


def make_server(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(24)
    holder = {"port": 0}
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(token, holder))
    holder["port"] = srv.server_address[1]
    return srv, token


def serve(open_browser: bool = True, port: int = 0) -> None:
    store.ensure_home()
    srv, token = make_server(port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/#token={token}"
    print(f"ai-bridge panel: {url}")
    print("Only reachable from this computer. Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nPanel stopped.")
    finally:
        srv.server_close()
