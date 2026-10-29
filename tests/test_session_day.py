import importlib.util
import json
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
REPLY = {"type": "assistant", "effort": "high", "message": {"id": "m1", "model": "claude-opus-5-5", "content": [], "usage": {"input_tokens": 10}}}


class Tomorrow(date):
    @classmethod
    def today(cls):
        return date.fromtimestamp(time.time()) + timedelta(days=1)


def keys_shown_tomorrow(status):
    """Keys get_sessions shows a day after a session last changed to `status`."""
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR, ccm.PROJECTS_DIR = tmp / "sessions", tmp / "projects"
    ccm.SESSIONS_DIR.mkdir()
    (ccm.PROJECTS_DIR / "p").mkdir(parents=True)
    now_ms = int(time.time() * 1000)
    (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps({"sessionId": "s1", "cwd": "C:/x", "status": status, "updatedAt": now_ms}))
    (ccm.PROJECTS_DIR / "p" / "s1.jsonl").write_text(json.dumps(REPLY) + "\n")
    ccm.date = Tomorrow
    return [s["key"] for s in ccm.get_sessions()]


def test_busy_since_before_midnight_stays():
    assert keys_shown_tomorrow("busy") == ["s1"]


def test_waiting_since_before_midnight_stays():
    assert keys_shown_tomorrow("waiting") == ["s1"]


def test_idle_since_before_midnight_drops():
    assert keys_shown_tomorrow("idle") == []


def test_spare_session_has_no_card():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR, ccm.PROJECTS_DIR = tmp / "sessions", tmp / "projects"
    ccm.SESSIONS_DIR.mkdir()
    ccm.PROJECTS_DIR.mkdir()
    (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps(
        {"sessionId": "s1", "cwd": "C:/x", "status": "busy", "updatedAt": int(time.time() * 1000), "spare": True}))
    assert ccm.get_sessions() == [], "a spare background session shows a card"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
