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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
