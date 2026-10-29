import importlib.util
import json
import tempfile
import time
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


def sessions_shown(*files):
    """Keys and labels get_sessions shows for these session files, all live in one folder."""
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR, ccm.PROJECTS_DIR = tmp / "sessions", tmp / "projects"
    ccm.SESSIONS_DIR.mkdir()
    ccm.PROJECTS_DIR.mkdir()
    now_ms = int(time.time() * 1000)
    for i, extra in enumerate(files):
        session = {"sessionId": f"s{i}", "cwd": "C:\\repo", "status": "busy", "updatedAt": now_ms + i, **extra}
        (ccm.SESSIONS_DIR / f"{i}.json").write_text(json.dumps(session), encoding="utf-8")
    return sorted((s["key"], s["label"]) for s in ccm.get_sessions())


def test_two_unnamed_sessions_in_one_folder_both_show():
    shown = sessions_shown({"kind": "interactive"}, {"kind": "interactive"})
    assert shown == [("s0", "repo"), ("s1", "repo")], f"two live unnamed sessions in one folder shown as {shown}"


def test_background_copy_merges_into_its_original():
    shown = sessions_shown({"kind": "bg", "name": "ARC"}, {"kind": "bg", "name": "ARC (2)"})
    assert shown == [("s1", "ARC")], f"a background copy 'ARC (2)' did not merge into 'ARC': {shown}"


def test_numbered_interactive_session_keeps_its_name():
    shown = sessions_shown({"kind": "interactive", "name": "CCM"}, {"kind": "interactive", "name": "CCM (2)"})
    assert shown == [("s0", "CCM"), ("s1", "CCM (2)")], f"an interactive 'CCM (2)' merged into 'CCM': {shown}"


def test_same_named_sessions_in_one_folder_merge():
    shown = sessions_shown({"kind": "interactive", "name": "CCM"}, {"kind": "interactive", "name": "CCM"})
    assert shown == [("s1", "CCM")], f"two sessions named 'CCM' in one folder not merged: {shown}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
