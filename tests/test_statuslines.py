import importlib.util
import json
import subprocess
import tempfile
import time
import types
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


def load_plain():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    return ccm


def test_git_bash_is_the_configured_one_else_bash_on_path():
    ccm = load_plain()
    ccm.os = types.SimpleNamespace(environ={"CLAUDE_CODE_GIT_BASH_PATH": "D:/git/bin/bash.exe"})
    assert ccm.get_git_bash() == "D:/git/bin/bash.exe"
    ccm.os = types.SimpleNamespace(environ={})
    ccm.shutil = types.SimpleNamespace(which=lambda name: None)
    assert ccm.get_git_bash() == "bash", "no git on PATH"


def test_context_window_comes_from_the_model_catalog_else_200k():
    ccm = load_plain()
    ccm.CACHE = Path(tempfile.mkdtemp())
    catalog = ccm.CACHE / "model-catalog"
    catalog.mkdir()
    (catalog / "a.json").write_text("{broken")
    (catalog / "b.json").write_text(json.dumps({"catalog": {"config": {"models": [{"id": "big", "runtime": {"max_input_tokens": 1000000}}]}}}))
    assert ccm.get_context_window("big") == 1000000
    assert ccm.get_context_window("other") == 200000


def test_statusline_input_carries_the_rate_limits_when_known():
    ccm = load_plain()
    ccm.CACHE = Path(tempfile.mkdtemp())
    limits = {"five_hour": {"used_percentage": 10}}
    assert ccm.build_statusline_input(info("s"), limits)["rate_limits"] == limits
    assert "rate_limits" not in ccm.build_statusline_input(info("s"), None)


def test_posix_timeout_kills_the_process_group_and_a_failed_read_gives_no_output():
    ccm = load_plain()
    killed = []

    class Process:
        pid = 42

        def __init__(self, error):
            self.error, self.killed = error, False

        def communicate(self, text=None, timeout=None):
            if timeout:
                raise self.error
            return "", None

        def kill(self):
            self.killed = True

    ccm.sys = types.SimpleNamespace(platform="linux")
    ccm.os = types.SimpleNamespace(killpg=lambda pid, signal: killed.append((pid, signal)))
    for error, kills in ((subprocess.TimeoutExpired("bash", 10), [(42, 9)]), (OSError("pipe closed"), [])):
        process = Process(error)
        ccm.subprocess = types.SimpleNamespace(**{**vars(subprocess), "Popen": lambda *args, **kwargs: process})
        killed.clear()
        assert ccm.run_statusline_command("bash", "x", "{}", "") == "", error
        assert (killed, process.killed) == (kills, bool(kills)), f"{error!r}: killed {killed}"


def test_cache_write_that_fails_is_skipped():
    ccm = load_plain()
    ccm.STATUSLINE_CACHE = Path(tempfile.mkdtemp()) / "gone" / "cache.json"
    ccm.save_statuslines({"s": ["x"]}, [])
    assert not ccm.STATUSLINE_CACHE.exists()


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
