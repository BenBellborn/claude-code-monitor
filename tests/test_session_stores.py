import importlib.util
import json
import tempfile
import time
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
REPLY = {"type": "assistant", "effort": "high", "message": {"id": "m1", "model": "claude-opus-5-5", "content": [], "usage": {"input_tokens": 10}}}


def stores_after_render(unreadable_session_file):
    """Store keys after one render, given a live session and what a session that ended yesterday left in every store."""
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR, ccm.PROJECTS_DIR = tmp / "sessions", tmp / "projects"
    ccm.SESSIONS_DIR.mkdir()
    project = ccm.PROJECTS_DIR / "p"
    (project / "live" / "subagents").mkdir(parents=True)
    now_ms = int(time.time() * 1000)
    (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps({"sessionId": "live", "cwd": "C:/x", "status": "idle", "updatedAt": now_ms}))
    if unreadable_session_file:
        (ccm.SESSIONS_DIR / "2.json").write_text('{"sessionId": "ended", "cw')  # read mid-rewrite
    (project / "live.jsonl").write_text(json.dumps(REPLY) + "\n")
    (project / "live" / "subagents" / "agent-a.meta.json").write_text(json.dumps({"agentType": "x"}))
    (project / "live" / "subagents" / "agent-a.jsonl").write_text("")
    ccm.statusline_inputs["ended"] = {}
    ccm.statuslines["ended"] = ["old"]
    ccm.scan_transcript(project / "ended.jsonl")
    ccm.scan_transcript(project / "ended" / "subagents" / "agent-b.jsonl")
    ccm.update_sessions()
    return {
        "statusline_inputs": set(ccm.statusline_inputs), "statuslines": set(ccm.statuslines),
        "transcript_totals": {p.relative_to(project).as_posix() for p in ccm.transcript_totals},
    }


def test_ended_session_leaves_no_keys_behind():
    stores = stores_after_render(unreadable_session_file=False)
    assert stores == {
        "statusline_inputs": {"live"}, "statuslines": set(),
        "transcript_totals": {"live.jsonl", "live/subagents/agent-a.jsonl"},
    }, f"stores keep ended sessions: {stores}"


def test_unreadable_session_file_keeps_every_key():
    stores = stores_after_render(unreadable_session_file=True)
    assert stores["statuslines"] == {"ended"}, f"a session file read mid-rewrite drops its footer: {stores}"
    assert "ended.jsonl" in stores["transcript_totals"], f"a session file read mid-rewrite drops its scan: {stores}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
