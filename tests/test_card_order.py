import importlib.util
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
REPLY = {"type": "assistant", "effort": "high", "message": {"id": "m1", "model": "claude-opus-5-5", "content": [], "usage": {"input_tokens": 10}}}


def test_cards_sort_by_status_then_most_cache_time_left_then_folder():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR, ccm.PROJECTS_DIR = tmp / "sessions", tmp / "projects"
    ccm.SESSIONS_DIR.mkdir()
    (ccm.PROJECTS_DIR / "p").mkdir(parents=True)
    now = int(time.time())
    stamp = lambda minutes_ago: datetime.fromtimestamp(now - minutes_ago * 60, timezone.utc).isoformat().replace("+00:00", "Z")
    cards = [("C:/a", "idle", 1), ("C:/b", "busy", 3), ("C:/c", "waiting", 2), ("C:/d", "busy", 1), ("C:/e", "busy", 3)]
    for i, (cwd, status, minutes_ago) in enumerate(cards):
        sid = f"s{i}"
        (ccm.SESSIONS_DIR / f"{i}.json").write_text(json.dumps({"sessionId": sid, "cwd": cwd, "status": status, "updatedAt": now * 1000}))
        (ccm.PROJECTS_DIR / "p" / f"{sid}.jsonl").write_text(json.dumps({**REPLY, "timestamp": stamp(minutes_ago)}) + "\n")
    assert [s["cwd"] for s in ccm.get_sessions()] == ["C:/c", "C:/d", "C:/b", "C:/e", "C:/a"]


if __name__ == "__main__":
    test_cards_sort_by_status_then_most_cache_time_left_then_folder()
    print("PASS")
