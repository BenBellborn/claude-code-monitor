import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
REPLY = {"type": "assistant", "effort": "high", "message": {"id": "m1", "model": "claude-opus-5-5", "content": [], "usage": {"input_tokens": 10}}}


def load():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR, ccm.PROJECTS_DIR, ccm.PROGRESS_DIR = tmp / "sessions", tmp / "projects", tmp / "progress"
    ccm.CLAUDE_JSON = tmp / ".claude.json"
    ccm.SESSIONS_DIR.mkdir()
    (ccm.PROJECTS_DIR / "p").mkdir(parents=True)
    ccm.PROGRESS_DIR.mkdir()
    return ccm


def keys_shown(**session):
    """Keys get_sessions shows for one idle session whose last status change was a week ago."""
    ccm = load()
    week_ago_ms = int((time.time() - 7 * 86400) * 1000)
    (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps({"sessionId": "s1", "cwd": "C:/x", "status": "idle", "updatedAt": week_ago_ms, **session}))
    (ccm.PROJECTS_DIR / "p" / "s1.jsonl").write_text(json.dumps(REPLY) + "\n")
    return [s["key"] for s in ccm.get_sessions()]


def test_running_session_idle_for_days_shows():
    assert keys_shown(pid=os.getpid()) == ["s1"]


def test_session_whose_process_exited_has_no_card_or_note():
    exited = subprocess.Popen([sys.executable, "-c", ""])
    exited.wait()
    ccm = load()
    note = ccm.PROGRESS_DIR / "ccm-progress-s1.txt"
    note.write_text("Input needed", encoding="utf-8")
    assert ccm.find_session({"sessionId": "s1", "pid": exited.pid, "status": "waiting"}) is None, "the card of a closed session stays"
    assert not note.exists(), "a closed session's note stays for a resume to show"


def test_session_whose_pid_was_reused_has_no_card():
    if sys.platform != "win32":
        return
    assert keys_shown(pid=os.getpid(), procStart="1") == [], "a process that reused the pid keeps a closed session's card"


def test_session_with_its_own_start_time_shows():
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    created, other = wintypes.FILETIME(), wintypes.FILETIME()
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetProcessTimes(wintypes.HANDLE(kernel32.GetCurrentProcess()), ctypes.byref(created), *(ctypes.byref(other) for _ in range(3)))
    assert keys_shown(pid=os.getpid(), procStart=str(created.dwHighDateTime << 32 | created.dwLowDateTime)) == ["s1"]


def test_card_takes_the_rename_title_over_a_made_up_session_name():
    ccm = load()
    title = {"type": "custom-title", "customTitle": "CoP", "sessionId": "s1"}
    (ccm.PROJECTS_DIR / "p" / "s1.jsonl").write_text(json.dumps(title) + "\n" + json.dumps(REPLY) + "\n")
    session = ccm.find_session({"sessionId": "s1", "cwd": "C:/x/ContentPlatform", "status": "idle", "kind": "bg", "name": "context-token-breakdown"})
    assert session["name"] == "CoP[BG]", session["name"]
    untitled = ccm.find_session({"sessionId": "s2", "cwd": "C:/x/ContentPlatform", "status": "idle", "name": "ARC (2)", "kind": "bg"})
    assert untitled["name"] == "ARC[BG]", "a session without a title lost its session file name"
    unnamed = ccm.find_session({"sessionId": "s3", "cwd": "C:/x/ContentPlatform", "status": "idle", "kind": "bg"})
    assert unnamed["name"] == "ContentPlatform[BG]", unnamed["name"]


def test_background_session_has_its_own_card_beside_its_original():
    ccm = load()
    for i, kind in enumerate(("interactive", "bg")):
        (ccm.SESSIONS_DIR / f"{i}.json").write_text(json.dumps(
            {"sessionId": f"s{i}", "cwd": "C:/x", "status": "idle", "name": "CoP", "kind": kind, "pid": os.getpid()}))
    assert sorted(s["label"] for s in ccm.get_sessions()) == ["CoP", "CoP[BG]"]


def cards_with_watcher(status, mcp_args=None):
    """get_sessions for one session of this test process while a mod's watcher script runs as its child."""
    ccm = load()
    cwd = ccm.SESSIONS_DIR.parent / "repo"
    script = ccm.SESSIONS_DIR.parent / "mods" / "review-watch" / "scripts" / "watch.py"
    script.parent.mkdir(parents=True)
    cwd.mkdir()
    script.write_text("import time\ntime.sleep(30)\n")
    if mcp_args:
        (cwd / ".mcp.json").write_text(json.dumps({"mcpServers": {"w": {"command": "python", "args": mcp_args}}}))
    (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps({"sessionId": "s1", "cwd": str(cwd), "status": status, "name": "ES", "pid": os.getpid()}))
    watcher = subprocess.Popen([sys.executable, str(script)])
    try:
        return ccm.get_sessions(), watcher.pid
    finally:
        watcher.kill()


def test_background_process_of_a_session_has_its_own_card():
    if sys.platform != "win32":
        return
    cards, pid = cards_with_watcher("idle")
    work = [c for c in cards if c["key"] == f"s1:{pid}"]
    assert work, [c["key"] for c in cards]
    assert work[0]["label"] == "ES[BG]" and work[0]["color"] == "working", work[0]
    assert work[0]["detail"] == "review-watch · watch.py · 0h 0m", work[0]["detail"]


def test_mcp_server_of_a_session_has_no_card():
    if sys.platform != "win32":
        return
    cards, _ = cards_with_watcher("idle", mcp_args=["watch.py"])
    assert [c["key"] for c in cards] == ["s1"], "an MCP server shows as background work"


def test_spare_session_has_no_card():
    ccm = load()
    (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps(
        {"sessionId": "s1", "cwd": "C:/x", "status": "busy", "updatedAt": int(time.time() * 1000), "spare": True}))
    assert ccm.get_sessions() == [], "a spare background session shows a card"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
