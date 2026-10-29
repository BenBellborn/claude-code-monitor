import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


def load():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    return ccm


def test_two_hooks_at_once_keep_both_sessions():
    ccm = load()
    ccm.STARTED_SESSIONS = Path(tempfile.mkdtemp()) / "ccm-started-sessions.txt"
    ccm.STARTED_SESSIONS.write_text("old")
    real_read = ccm.read_started_sessions
    both_read = threading.Barrier(2, timeout=1)

    def read_then_wait():  # both hooks read before either writes, unless a lock keeps the second out
        started = real_read()
        try:
            both_read.wait()
        except threading.BrokenBarrierError:
            pass
        return started

    ccm.read_started_sessions = read_then_wait
    threads = [threading.Thread(target=ccm.add_started_session, args=(s,)) for s in ("A", "B")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(ccm.STARTED_SESSIONS.read_text().split()) == ["A", "B", "old"], ccm.STARTED_SESSIONS.read_text().split()


def test_exit_mid_write_keeps_the_statusline_cache():
    tmp = Path(tempfile.mkdtemp())
    cache = tmp / "sl.json"
    cache.write_text(json.dumps({"sessions": {"s1": ["kept"]}, "shared": ["5h:1%"]}))
    child = textwrap.dedent(f"""
        import importlib.util, os, pathlib
        spec = importlib.util.spec_from_file_location("c", {str(CCM_PATH)!r}); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        m.STATUSLINE_CACHE = pathlib.Path({str(cache)!r}); m.CLAUDE_DIR = pathlib.Path({str(tmp)!r})
        real_open = pathlib.Path.open
        class ExitOnWrite:
            def __init__(s, f): s.f = f
            def __enter__(s): return s
            def __exit__(s, *a): s.f.close()
            def write(s, d): os._exit(0)
        pathlib.Path.open = lambda self, mode='r', *a, **k: ExitOnWrite(real_open(self, mode, *a, **k)) if 'w' in mode else real_open(self, mode, *a, **k)
        m.run_statuslines()
    """)
    subprocess.run([sys.executable, "-I", "-c", child], check=True, timeout=30)
    assert load().read_json(cache) is not None, f"cache after exit: {cache.read_bytes()!r}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
