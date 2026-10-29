import importlib.util
import json
import tempfile
import time
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


class Stop(Exception):
    pass


def load(command, outputs):
    """ccm on a temp settings.json and cache; the statusLine command prints outputs[session id] and records the session it ran for."""
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.CLAUDE_DIR = ccm.CACHE = tmp
    ccm.STATUSLINE_CACHE = tmp / "cache.json"
    if command:
        (tmp / "settings.json").write_text(json.dumps({"statusLine": {"command": command}}))
    calls = []

    def run(bash, command, text):
        session = json.loads(text)["session_id"]
        calls.append(session)
        return outputs[session]

    ccm.run_statusline_command = run
    return ccm, calls


def info(session):
    return {"session_id": session, "transcript_path": "", "cwd": "C:/x", "model": "m", "context_tokens": 0}


def one_pass(ccm):
    """One run_statuslines iteration: the sleep that ends it stops the loop."""
    real_sleep = time.sleep
    time.sleep = lambda seconds: (_ for _ in ()).throw(Stop())
    try:
        ccm.run_statuslines()
    except Stop:
        pass
    finally:
        time.sleep = real_sleep


def test_removed_statusline_clears_the_cache():
    ccm, calls = load(None, {})
    ccm.STATUSLINE_CACHE.write_text(json.dumps({"sessions": {"s": ["old footer"]}, "shared": ["5h:10%"]}))
    one_pass(ccm)
    saved = json.loads(ccm.STATUSLINE_CACHE.read_text())
    assert saved == {"sessions": {}, "shared": []}, f"cache after run: {saved}"
    assert (ccm.statuslines, ccm.global_parts) == ({}, []), f"shown after run: {ccm.statuslines}, {ccm.global_parts}"


def test_run_that_prints_nothing_clears_its_parts():
    ccm, calls = load("x", {"s": ""})
    ccm.STATUSLINE_CACHE.write_text(json.dumps({"sessions": {"s": ["old footer"], "gone": ["x"]}, "shared": ["5h:10%"]}))
    ccm.statusline_inputs["s"] = info("s")
    one_pass(ccm)
    saved = json.loads(ccm.STATUSLINE_CACHE.read_text())
    assert saved == {"sessions": {"s": []}, "shared": []}, f"cache after run: {saved}"
    assert (ccm.statuslines["s"], ccm.global_parts) == ([], []), f"shown after run: {ccm.statuslines}, {ccm.global_parts}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
