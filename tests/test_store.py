import multiprocessing
import os
import stat

import pytest

from ai_bridge import store


def test_append_and_read_assigns_increasing_ids():
    store.append("claude", "one")
    store.append("gemini", "two", "t1")
    ids = [e["id"] for e in store.read_all()]
    assert ids == [1, 2]


def test_log_file_is_private():
    store.append("claude", "secret-ish")
    assert stat.S_IMODE(os.stat(store.LOG_FILE).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.HOME).st_mode) == 0o700


def test_reading_one_thread_does_not_skip_other_threads():
    store.append("human", "in a", "a")
    store.append("human", "in b", "b")
    assert [e["text"] for e in store.read("claude", thread="b")] == ["in b"]
    # Regression: the earlier global cursor skipped message #1 here
    assert [e["text"] for e in store.read("claude")] == ["in a", "in b"]
    assert store.read("claude") == []
    assert [e["text"] for e in store.read("claude", thread="a")] == ["in a"]


def test_cursors_are_per_agent():
    store.append("human", "hello")
    assert len(store.read("claude")) == 1
    assert len(store.read("gemini")) == 1


@pytest.mark.parametrize("bad", ["Bad Name", "x" * 41, "-start", "a/b"])
def test_invalid_thread_names(bad):
    with pytest.raises(ValueError):
        store.append("claude", "x", bad)


def test_message_limits_and_unknown_agent():
    with pytest.raises(ValueError):
        store.append("claude", "   ")
    with pytest.raises(ValueError):
        store.append("claude", "x" * (store.MAX_TEXT + 1))
    with pytest.raises(ValueError):
        store.append("mallory", "hi")


def test_switch():
    store.set_enabled(False)
    with pytest.raises(store.BridgeOff):
        store.require_on()
    store.set_enabled(True)
    store.require_on()


def _writer(n):
    for i in range(n):
        store.append("claude", f"msg {i}")


def test_concurrent_appends_get_unique_ids():
    procs = [multiprocessing.Process(target=_writer, args=(25,)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    ids = [e["id"] for e in store.read_all()]
    assert len(ids) == 100
    assert sorted(ids) == list(range(1, 101))
