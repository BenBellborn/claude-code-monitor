import importlib.util
import json
import tempfile
import time
from datetime import date
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
ccm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccm)

REAL = {"type": "assistant", "effort": "high", "message": {"id": "m1", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 90000, "output_tokens": 5}}}
SYNTHETIC = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "message": {"id": "m2", "model": "<synthetic>",
             "content": [{"type": "text", "text": "You've hit your org's monthly spend limit"}],
             "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}


def scan(*entries):
    path = Path(tempfile.mkdtemp()) / "t.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return ccm.scan_transcript(path)


def test_synthetic_reply_keeps_last_real_values():
    totals = scan(REAL, SYNTHETIC)
    assert (totals["model"], totals["effort"], totals["context"]) == ("claude-opus-5-5", "high", 90010), totals


def test_line_longer_than_old_tail_window_keeps_last_reply():
    assistant = {"type": "assistant", "effort": "high", "message": {"id": "m1", "model": "claude-opus-5-5", "content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "C:/x/shot.png"}}], "usage": {"input_tokens": 1000}}}
    result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "A" * (600 * 1024)}]}}
    totals = scan(assistant, result)
    got = (totals["model"], totals["effort"], totals["activity"], totals["context"])
    assert got == ("claude-opus-5-5", "high", "Read: shot.png", 1000), got


def test_silent_tool_keeps_previous_activity():
    read = {"type": "assistant", "message": {"id": "m1", "model": "m", "content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}]}}
    handback = {"type": "assistant", "message": {"id": "m2", "model": "m", "content": [{"type": "tool_use", "name": "SubagentHandback", "input": {}}]}}
    assert scan(read, handback)["activity"] == "Grep: x"


def test_progress_note_shows_only_while_busy_or_waiting():
    ccm.PROGRESS_DIR = ccm.PROJECTS_DIR = Path(tempfile.mkdtemp())
    (ccm.PROGRESS_DIR / "ccm-progress-s1.txt").write_text("Step 2/4: tests\n", encoding="utf-8")

    def note(session_id, status):
        return ccm.find_session({"sessionId": session_id, "status": status, "updatedAt": time.time() * 1000}, date.today())["note"]

    got = [note("s1", "busy"), note("s1", "waiting"), note("s1", "idle"), note("s2", "busy")]
    assert got == ["Step 2/4: tests", "Step 2/4: tests", "", ""], got


def test_missing_transcript_gives_defaults():
    totals = ccm.scan_transcript(Path(tempfile.mkdtemp()) / "none.jsonl")
    assert (totals["model"], totals["effort"], totals["context"], totals["activity"]) == ("?", "?", 0, None), totals


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
