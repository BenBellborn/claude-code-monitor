import importlib.util
import json
import os
import tempfile
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
ccm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccm)

REPLY = {"type": "assistant", "effort": "high", "message": {"id": "m1", "model": "claude-opus-5-5", "stop_reason": "end_turn",
         "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 10, "output_tokens": 5}}}
CALL = {"type": "assistant", "message": {"id": "m2", "model": "claude-opus-5-5", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}]}}
RESULT = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "found"}]}}


def subagent_rows(*entries):
    """Rows for one subagent with no "finished" in its meta, whose transcript holds the given entries."""
    tmp = Path(tempfile.mkdtemp())
    transcript = tmp / "s1.jsonl"
    transcript.write_text("")
    sub = tmp / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.meta.json").write_text(json.dumps({"agentType": "verifier", "description": "d", "taskKind": "in_process_teammate"}))
    (sub / "agent-x.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    return ccm.get_active_subagents(transcript)


def test_idle_teammate_has_no_row():
    assert subagent_rows(REPLY) == []


def test_subagent_running_a_tool_has_a_row():
    rows = subagent_rows(REPLY, {"type": "user", "message": {"content": "next task"}}, CALL, RESULT)
    assert [r["activity"] for r in rows] == ["Grep: x"], rows


def test_subagent_stopped_mid_call_long_ago_has_no_row():
    tmp = Path(tempfile.mkdtemp())
    sub = tmp / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-old.meta.json").write_text(json.dumps({"agentType": "verifier"}))
    (sub / "agent-old.jsonl").write_text(json.dumps(CALL) + "\n")
    for f in sub.iterdir():
        os.utime(f, (0, 0))
    assert ccm.get_active_subagents(tmp / "s1.jsonl") == [], "a subagent silent for hours still shows as working"


def test_unreadable_or_finished_meta_has_no_row_and_the_next_still_shows():
    tmp = Path(tempfile.mkdtemp())
    sub = tmp / "s1" / "subagents"
    sub.mkdir(parents=True)
    (sub / "a-bad.meta.json").write_text("{")
    (sub / "b-folder.meta.json").mkdir()
    (sub / "c-done.meta.json").write_text(json.dumps({"finished": True}))
    (sub / "d-live.meta.json").write_text(json.dumps({"agentType": "general-purpose", "description": "live"}))
    rows = ccm.get_active_subagents(tmp / "s1.jsonl")
    assert [(r["type"], r["activity"]) for r in rows] == [("", "live")], rows


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
