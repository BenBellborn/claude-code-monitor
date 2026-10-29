import importlib.util
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


def load():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    return ccm


def test_a_broken_session_file_skips_only_that_session():
    ccm = load()
    root = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR = root / "sessions"
    ccm.PROJECTS_DIR = root / "projects"
    ccm.SESSIONS_DIR.mkdir()
    ccm.PROJECTS_DIR.mkdir()
    now = time.time() * 1000
    (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps({"cwd": "C:/x", "status": "busy", "updatedAt": now}), encoding="utf-8")
    (ccm.SESSIONS_DIR / "2.json").write_text(json.dumps({"sessionId": "ok", "cwd": "C:/y", "status": "busy", "updatedAt": now}), encoding="utf-8")
    try:
        sessions = ccm.get_sessions()
    except Exception as e:
        raise AssertionError(f"one broken session file ends the render: {e!r}")
    assert [s["key"] for s in sessions] == ["ok"], sessions


def test_a_null_cwd_does_not_end_the_render():
    ccm = load()
    root = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR = root / "sessions"
    ccm.PROJECTS_DIR = root / "projects"
    ccm.SESSIONS_DIR.mkdir()
    ccm.PROJECTS_DIR.mkdir()
    (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps(
        {"sessionId": "n", "name": "x", "cwd": None, "status": "busy", "updatedAt": time.time() * 1000}), encoding="utf-8")
    assert [s["key"] for s in ccm.get_sessions()] == ["n"]


def test_watcher_retries_a_read_that_failed():
    ccm = load()
    reads = [0]

    class Script:  # an edit whose first read hits a lock
        def stat(self):
            return types.SimpleNamespace(st_mtime_ns=1)

        def read_text(self, encoding):
            reads[0] += 1
            if reads[0] == 1:
                raise PermissionError("locked")
            return "x = 1\n"

        def __str__(self):
            return "ccm.py"

    class Restarted(Exception):
        pass

    def restart(_):
        raise Restarted

    calls = [0]

    def sleep(_):
        calls[0] += 1
        if calls[0] > 3:
            raise AssertionError("the edit was never loaded")

    ccm.SCRIPT = Script()
    ccm.time = types.SimpleNamespace(sleep=sleep)
    ccm.start_with_instance_claim = restart
    try:
        ccm.restart_on_new_version(0)
    except Restarted:
        pass


def test_watcher_restarts_on_the_same_port_and_claim_once_the_script_changes():
    ccm = load()
    ccm.SCRIPT = Path(tempfile.mkdtemp()) / "ccm.py"
    ccm.SCRIPT.write_text("x = 1\n")
    ccm.VERSION = str(ccm.SCRIPT.stat().st_mtime_ns)
    ccm.instance_lock = 7
    polls, started = [0], []

    class Exited(Exception):
        pass

    def sleep(_):
        polls[0] += 1
        if polls[0] == 2:  # the first poll sees the running version
            os.utime(ccm.SCRIPT, ns=(0, 5))

    def exit(_):
        raise Exited

    ccm.time = types.SimpleNamespace(sleep=sleep)
    ccm.start_with_instance_claim = started.append
    ccm.os = types.SimpleNamespace(_exit=exit)
    try:
        ccm.restart_on_new_version(8123)
    except Exited:
        pass
    assert polls[0] == 2, f"restarted after {polls[0]} polls"
    assert started == [[sys.executable, str(ccm.SCRIPT), "--reloaded", "8123", "7"]], started


def test_watcher_keeps_polling_while_the_script_is_gone():
    ccm = load()
    ccm.SCRIPT = Path(tempfile.mkdtemp()) / "ccm.py"  # mid save-by-rename: the file is briefly gone
    calls = [0]

    class Stop(Exception):
        pass

    def sleep(_):
        calls[0] += 1
        if calls[0] > 2:
            raise Stop

    ccm.time = types.SimpleNamespace(sleep=sleep)
    try:
        ccm.restart_on_new_version(0)
    except Stop:
        pass
    assert calls[0] == 3, "watcher died"


def test_watcher_keeps_running_on_an_edit_that_does_not_decode():
    ccm = load()
    ccm.SCRIPT = Path(tempfile.mkdtemp()) / "ccm.py"
    ccm.SCRIPT.write_bytes(b"x = '\xff'\n")
    calls = [0]

    class Stop(Exception):
        pass

    def sleep(_):
        calls[0] += 1
        if calls[0] > 2:
            raise Stop

    ccm.time = types.SimpleNamespace(sleep=sleep)
    try:
        ccm.restart_on_new_version(0)
    except Stop:
        pass
    assert calls[0] == 3, "watcher died"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
