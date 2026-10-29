import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


def test_prompt_starts_a_monitor_until_one_ran():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.CACHE = tmp
    ccm.STARTED_SESSIONS = tmp / "ccm-started-sessions.txt"

    def hook(event):
        sys.stdin = io.StringIO(json.dumps({"session_id": "s1", "hook_event_name": event}))
        return ccm.find_hook_session()

    assert hook("SessionStart") == "s1"
    # The detached monitor then fails before it opens a window. No monitor runs.
    assert hook("UserPromptSubmit") == "s1", "prompt hook refuses to start a monitor: session marked started though none ran"
    ccm.add_started_session("s1")
    assert hook("UserPromptSubmit") is None, "prompt hook starts a second monitor for a session whose monitor ran"
    assert hook("SessionStart") == "s1", "a session start (resume) must always start a monitor"


def test_prompt_after_clear_keeps_a_closed_window_closed():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.CACHE = tmp
    ccm.STARTED_SESSIONS = tmp / "ccm-started-sessions.txt"

    def hook(session, event, source=None):
        sys.stdin = io.StringIO(json.dumps({"session_id": session, "hook_event_name": event, "source": source}))
        return ccm.find_hook_session()

    ccm.add_started_session("before-clear")  # its window opened, then the user closed it
    clear_started = hook("after-clear", "SessionStart", "clear")
    assert hook("after-clear", "UserPromptSubmit") is None, "first prompt after /clear starts the monitor again"
    assert clear_started is None, "/clear reopens the window"
    hooks = json.loads((CCM_PATH.parent / "hooks" / "hooks.json").read_text())
    assert "clear" in hooks["hooks"]["SessionStart"][0]["matcher"].split("|"), "SessionStart never fires on /clear"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
