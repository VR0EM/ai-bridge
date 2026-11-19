import http.client
import json
import threading

import pytest

from ai_bridge import config, store, ui


@pytest.fixture
def panel(fake_agy):
    srv, token = ui.make_server()
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]

    def call(method, path, body=None, tok=token, host=None, ctype="application/json"):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        headers = {"Host": host or f"127.0.0.1:{port}"}
        if tok:
            headers["X-Bridge-Token"] = tok
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = ctype
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except json.JSONDecodeError:
            return r.status, raw.decode()

    yield call
    srv.shutdown()


def test_page_loads_without_token_but_api_needs_it(panel):
    status, html = panel("GET", "/", tok=None)
    assert status == 200 and "ai-bridge" in html
    assert panel("GET", "/api/state", tok=None)[0] == 401
    assert panel("GET", "/api/state", tok="wrong")[0] == 401


def test_wrong_host_is_refused(panel):
    assert panel("GET", "/api/state", host="evil.example:80")[0] == 403


def test_state_switch_and_log(panel):
    status, s = panel("GET", "/api/state")
    assert status == 200 and s["config"]["gemini_model"].startswith("gemini-")
    assert panel("POST", "/api/on", {})[0] == 200 and store.is_enabled()
    store.append("human", "hello from the test")
    _, log = panel("GET", "/api/log?since=0")
    assert log["entries"][-1]["text"] == "hello from the test"
    status, r = panel("POST", "/api/off", {})
    assert status == 200 and not store.is_enabled()


def test_settings_are_validated(panel):
    status, r = panel("POST", "/api/config", {"max_parallel_jobs": 99})
    assert status == 400 and "max_parallel_jobs" in r["error"]
    status, r = panel("POST", "/api/config", {"max_parallel_jobs": 2, "deny_commands": ["ssh", "qm"]})
    assert status == 200 and config.load()["deny_commands"] == ["ssh", "qm"]
    assert panel("POST", "/api/config/reset", {})[0] == 200
    assert config.load()["max_parallel_jobs"] == config.DEFAULTS["max_parallel_jobs"]


def test_writes_need_json(panel):
    assert panel("POST", "/api/on", {}, ctype="text/plain")[0] == 400
