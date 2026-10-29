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

    def run(bash, command, text, cwd):
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


def test_shared_parts_merge_across_sessions():
    ccm, calls = load("x", {"a": "dir | 5h:10% | rtk 50%, codemap 60%", "b": "dir | 5h:20%"})
    ccm.statusline_inputs.update({"a": info("a"), "b": info("b")})
    one_pass(ccm)
    assert ccm.global_parts == ["5h:20%", "rtk 50%, codemap 60%"], f"global_parts: {ccm.global_parts}"


def test_ansi_colours_become_spans():
    ccm, calls = load("x", {"a": "\x1b[31mred<\x1b[0m plain | 5h:\x1b[33m1%\x1b[9mx"})
    ccm.statusline_inputs["a"] = info("a")
    one_pass(ccm)
    assert ccm.statuslines["a"] == ['<span class="red">red&lt;</span> plain'], ccm.statuslines["a"]
    assert ccm.global_parts == ['5h:<span class="yellow">1%</span>x'], ccm.global_parts


def test_statusline_runs_in_session_cwd():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.CLAUDE_DIR = ccm.CACHE = tmp
    ccm.STATUSLINE_CACHE = tmp / "cache.json"
    (tmp / "settings.json").write_text(json.dumps({"statusLine": {"command": 'basename "$PWD"'}}))
    (tmp / "sessiondir").mkdir()
    ccm.statusline_inputs["s"] = {**info("s"), "cwd": str(tmp / "sessiondir")}
    one_pass(ccm)
    assert ccm.statuslines["s"] == ["sessiondir"], ccm.statuslines["s"]


def test_failed_start_gives_no_output():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    bash = ccm.get_git_bash()
    assert ccm.run_statusline_command(bash, "cat", "{}", "") == "{}"
    assert ccm.run_statusline_command(str(Path(tempfile.mkdtemp()) / "no-bash"), "cat", "{}", "") == "", "missing bash"
    assert ccm.run_statusline_command(bash, "echo \0x", "{}", "") == "", "NUL in command"
    assert ccm.run_statusline_command(bash, "cat", "{}", str(Path(tempfile.mkdtemp()) / "gone")) == "", "missing cwd"


def test_timeout_returns_while_a_grandchild_holds_stdout():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    start = time.monotonic()
    out = ccm.run_statusline_command(ccm.get_git_bash(), "sleep 25; echo x", "{}", "")
    elapsed = time.monotonic() - start
    assert (out, elapsed < 15) == ("", True), f"returned {out!r} after {elapsed:.1f} s"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
