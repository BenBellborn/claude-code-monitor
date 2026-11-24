import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path

import pytest

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
RESETS_AT = 1_800_000_000
LIMIT = {"type": "assistant", "uuid": "u1", "isSidechain": False, "isApiErrorMessage": True, "apiError": "usage_limit_reached",
         "apiErrorParams": {"rate_limit_info": {"resetsAt": RESETS_AT, "rateLimitType": "five_hour"}},
         "message": {"id": "m1", "model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your limit"}]}}
PROMPT = {"type": "user", "origin": {"kind": "human"}, "timestamp": "2026-11-14T10:00:00Z", "message": {"content": "go on"}}


def load():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR, ccm.PROJECTS_DIR, ccm.PROGRESS_DIR, ccm.CACHE = tmp / "sessions", tmp / "projects", tmp / "progress", tmp
    (ccm.PROJECTS_DIR / "p").mkdir(parents=True)
    ccm.PROGRESS_DIR.mkdir()
    return ccm


def write_transcript(ccm, *entries):
    transcript = ccm.PROJECTS_DIR / "p" / "s1.jsonl"
    with transcript.open("a", encoding="utf-8") as f:
        f.writelines(json.dumps(e) + "\n" for e in entries)
    return transcript


def test_limit_entry_sets_limit_stop_and_a_later_prompt_clears_it():
    ccm = load()
    transcript = write_transcript(ccm, LIMIT)
    assert ccm.scan_transcript(transcript)["limit_stop"] == {"uuid": "u1", "resets_at": RESETS_AT}
    write_transcript(ccm, PROMPT)
    assert ccm.scan_transcript(transcript)["limit_stop"] is None, "a prompt after the limit keeps the old limit stop"


def test_task_notification_after_the_limit_keeps_the_limit_stop():
    ccm = load()
    notification = {"type": "user", "timestamp": "2026-11-14T10:00:00Z",
                    "message": {"content": "<task-notification><status>completed</status></task-notification>"}}
    transcript = write_transcript(ccm, LIMIT, notification)
    assert ccm.scan_transcript(transcript)["limit_stop"] == {"uuid": "u1", "resets_at": RESETS_AT}


def test_limit_entry_with_no_reset_time_replaces_the_limit_stop():
    ccm = load()
    notification = {"type": "user", "timestamp": "2026-11-14T10:00:00Z",
                    "message": {"content": "<task-notification><status>completed</status></task-notification>"}}
    no_reset = {k: v for k, v in LIMIT.items() if k != "apiErrorParams"} | {"uuid": "u2"}
    transcript = write_transcript(ccm, LIMIT, notification, no_reset)
    assert ccm.scan_transcript(transcript)["limit_stop"] is None, "a limit entry with no reset time keeps the old limit stop"


def test_subagent_limit_entry_sets_no_limit_stop():
    ccm = load()
    transcript = write_transcript(ccm, {**LIMIT, "isSidechain": True})
    assert ccm.scan_transcript(transcript)["limit_stop"] is None


def test_idle_session_at_a_limit_shows_when_it_continues():
    ccm = load()
    write_transcript(ccm, LIMIT)
    card = ccm.find_session({"sessionId": "s1", "pid": os.getpid(), "status": "idle", "cwd": "C:/x"})
    assert card["note"] == f"Continues at {datetime.fromtimestamp(RESETS_AT + 60):%H:%M}"
    assert card["limit_stop"] == {"uuid": "u1", "resets_at": RESETS_AT} and card["pid"] == os.getpid()


@pytest.mark.parametrize("status", ["busy", "waiting"])
def test_session_at_a_limit_with_a_progress_note_still_gets_the_limit_stop(status):
    ccm = load()
    write_transcript(ccm, LIMIT)
    (ccm.PROGRESS_DIR / "ccm-progress-s1.txt").write_text("Round 2 · Step 1/4: applying plans", encoding="utf-8")
    card = ccm.find_session({"sessionId": "s1", "pid": os.getpid(), "status": status, "cwd": "C:/x"})
    assert card["limit_stop"] == {"uuid": "u1", "resets_at": RESETS_AT}, f"{status} session with a progress note lost its limit stop"
    assert card["note"] == f"Continues at {datetime.fromtimestamp(RESETS_AT + 60):%H:%M}"


def test_send_continue_types_continue_and_enter_into_the_session_console():
    if sys.platform != "win32":
        return
    ccm = load()
    hidden = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=0)
    # A redirected stdout makes Popen pass this process's stdin too, so the child reads its own console's input by name.
    child = subprocess.Popen([sys.executable, "-c", "print(open('CONIN$').readline())"], stdout=subprocess.PIPE, text=True,
                             creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=hidden)
    try:
        time.sleep(0.5)
        ccm.send_continue(child.pid)
        out, _ = child.communicate(timeout=10)
    finally:
        child.kill()
    assert out.strip() == "continue"


def test_continue_loop_goes_on_after_an_error_in_one_pass():
    class StopLoop(Exception):
        pass

    ccm = load()
    sent, sleeps = [], []

    def send_continue(pid):
        if not sent:
            sent.append(None)
            raise OSError("no console")
        sent.append(pid)

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopLoop

    ccm.update_sessions = lambda: [{"pid": 1, "limit_stop": {"uuid": "u1", "resets_at": 0}}]
    ccm.send_continue = send_continue
    ccm.time = types.SimpleNamespace(time=time.time, sleep=sleep)
    with pytest.raises(StopLoop):
        ccm.continue_limited_sessions()
    assert sent == [None, 1], "an error in one pass ends the continue loop"
