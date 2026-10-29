import importlib.util
import io
import json
import os
import subprocess
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


def test_rerun_on_consumed_stdin_starts_no_monitor():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.CACHE = tmp
    ccm.STARTED_SESSIONS = tmp / "ccm-started-sessions.txt"
    ccm.add_started_session("S1")  # S1's window opened, then the user closed it
    sys.stdin = io.StringIO(json.dumps({"session_id": "S1", "hook_event_name": "UserPromptSubmit"}))
    assert ccm.find_hook_session() is None
    sys.stdin = io.StringIO("")  # a second run after the first consumed stdin
    assert ccm.find_hook_session() is None, "rerun on empty stdin starts the monitor"


def test_hook_command_runs_ccm_once_when_it_fails():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "runs.log"
    hooks = json.loads((CCM_PATH.parent / "hooks" / "hooks.json").read_text())
    # python3 either works (and ccm then fails) or is a stub that runs nothing, like the WindowsApps alias.
    for python3_works, expected in ((True, ["python3"]), (False, ["python"])):
        for name in ("python3", "python"):
            works = python3_works or name == "python"
            (tmp / name).write_text(
                f'#!/bin/sh\n{"" if works else "exit 126"}\n[ "$1" = -c ] && exit 0\necho {name} >> "$RUNS"\nexit 1\n', newline="\n")
            (tmp / name).chmod(0o755)
        for event, entries in hooks["hooks"].items():
            log.unlink(missing_ok=True)
            subprocess.run(
                [ccm.find_git_bash(), "-c", entries[0]["hooks"][0]["command"]], input="", check=True, timeout=30,
                env={**os.environ, "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}", "RUNS": str(log), "CLAUDE_PLUGIN_ROOT": str(tmp)},
            )
            runs = log.read_text().split() if log.exists() else []
            assert runs == expected, f"{event} hook ran ccm as {runs}, expected {expected}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
