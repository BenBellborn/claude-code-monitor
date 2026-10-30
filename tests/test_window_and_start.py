import importlib.util
import os
import socket
import sys
import tempfile
import threading
import types
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


def load():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    return ccm


def test_window_opens_as_a_browser_tab():
    ccm = load()
    opened = []
    ccm.webbrowser = types.SimpleNamespace(open_new_tab=opened.append)
    ccm.open_monitor_window(8080)
    assert opened == ["http://127.0.0.1:8080/"], opened


def test_free_server_skips_a_taken_port():
    ccm = load()
    taken = socket.socket()
    taken.bind((ccm.HOST, 0))
    taken.listen()
    ccm.FIRST_PORT = taken.getsockname()[1]
    server = ccm.start_free_server()
    try:
        assert server.server_address[1] != ccm.FIRST_PORT, "bound the taken port"
    finally:
        server.server_close()
        taken.close()


def test_no_free_port_raises():
    ccm = load()

    def taken(*args):
        raise OSError("in use")

    ccm.MonitorServer = taken
    try:
        ccm.start_free_server()
    except OSError as e:
        assert "No free port" in str(e), e
    else:
        raise AssertionError("no error with every port taken")


def test_reloaded_server_waits_for_the_old_process_to_let_the_port_go():
    ccm = load()
    old = socket.socket()
    old.bind((ccm.HOST, 0))
    old.listen()
    port = old.getsockname()[1]
    threading.Timer(0.6, old.close).start()
    server = ccm.start_reloaded_server(port)
    server.server_close()
    assert server.server_address[1] == port


def test_reloaded_server_gives_up_after_its_wait():
    ccm = load()
    tries = [0]

    def taken(*args):
        tries[0] += 1
        raise OSError("in use")

    ccm.MonitorServer = taken
    ccm.time = types.SimpleNamespace(sleep=lambda seconds: None)
    try:
        ccm.start_reloaded_server(8080)
    except OSError:
        pass
    else:
        raise AssertionError("no error with the port never let go")
    assert tries[0] == 41, tries[0]


def test_posix_claim_is_a_lock_only_one_process_holds():
    ccm = load()
    ccm.CACHE = Path(tempfile.mkdtemp())
    held = []

    def flock(fd, flags):
        if held:
            raise BlockingIOError
        held.append(fd)

    ccm.sys = types.SimpleNamespace(platform="linux")
    sys.modules["fcntl"] = types.SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4)
    try:
        assert ccm.try_claim_instance() and ccm.instance_lock == held[0]
        assert not ccm.try_claim_instance(), "a second monitor took a held lock"
    finally:
        del sys.modules["fcntl"]
        os.close(held[0])


def load_for_start(claimed=True, claimed_after_wait=False):
    """ccm whose start-up steps record themselves in calls instead of running."""
    ccm = load()
    ccm.CACHE = Path(tempfile.mkdtemp()) / "cache"
    calls = []

    class Server:
        server_address = (ccm.HOST, 8123)

        def serve_forever(self):
            raise KeyboardInterrupt

    def thread(target, daemon, args=()):
        return types.SimpleNamespace(start=lambda: calls.append(target.__name__))

    def start_free_server():
        calls.append("free")
        return Server()

    def start_reloaded_server(port):
        calls.append(("reloaded", port))
        return Server()

    ccm.start_free_server = start_free_server
    ccm.start_reloaded_server = start_reloaded_server
    ccm.open_monitor_window = lambda port: calls.append(("window", port))
    ccm.threading = types.SimpleNamespace(Thread=thread)
    ccm.try_claim_instance = lambda: claimed
    ccm.claim_single_instance = lambda: claimed_after_wait
    return ccm, calls


def run_main(ccm, *args, detached=True):
    """Runs ccm's __main__ block on ccm's own globals, at its line numbers in ccm.py; "exited" when it calls sys.exit."""
    source = CCM_PATH.read_text(encoding="utf-8")
    start = source.index('if __name__ == "__main__":')
    code = compile("\n" * source.count("\n", 0, start) + source[start:], str(CCM_PATH), "exec")
    argv, flag = sys.argv, os.environ.pop("CCM_DETACHED", None)
    sys.argv = [str(CCM_PATH), *args]
    if detached:
        os.environ["CCM_DETACHED"] = "1"
    ccm.__name__ = "__main__"
    try:
        exec(code, vars(ccm))
    except SystemExit:
        return "exited"
    finally:
        sys.argv = argv
        os.environ.pop("CCM_DETACHED", None)
        if flag is not None:
            os.environ["CCM_DETACHED"] = flag


THREADS = ["restart_on_new_version", "run_statuslines", "exit_when_window_closed"]


def test_start_relaunches_itself_detached():
    ccm, calls = load_for_start()
    started = []
    ccm.subprocess = types.SimpleNamespace(Popen=lambda command, env, **kwargs: started.append((command, env["CCM_DETACHED"])))
    assert run_main(ccm, "--x", detached=False) == "exited"
    [(command, flag)] = started
    assert (command[1:], flag) == ([str(ccm.SCRIPT), "--x"], "1"), started
    assert Path(command[0]).name in ("pythonw.exe", Path(sys.executable).name), command[0]
    assert calls == [], "the hook's own process went on to start a server"


def test_new_monitor_opens_its_window_on_the_bound_port():
    ccm, calls = load_for_start()
    run_main(ccm)
    assert calls == ["free", ("window", 8123), *THREADS], calls
    assert ccm.CACHE.is_dir()


def test_monitor_that_waited_for_the_claim_starts():
    ccm, calls = load_for_start(claimed=False, claimed_after_wait=True)
    run_main(ccm)
    assert calls == ["free", ("window", 8123), *THREADS], calls


def test_start_with_another_monitor_running_exits():
    ccm, calls = load_for_start(claimed=False)
    assert run_main(ccm) == "exited"
    assert calls == [], calls


def test_reloaded_monitor_keeps_its_port_claim_and_window():
    ccm, calls = load_for_start(claimed=False)
    run_main(ccm, "--reloaded", "8123", "77")
    assert calls == [("reloaded", 8123), *THREADS], calls
    assert ccm.instance_lock == 77


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
