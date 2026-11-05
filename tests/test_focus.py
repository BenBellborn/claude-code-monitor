import http.client
import importlib.util
import json
import tempfile
import threading
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
ccm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccm)


def test_window_is_the_nearest_ancestor_that_has_one():
    parents = {10: 20, 20: 30, 30: 40, 40: 0}
    assert ccm.find_window_of_ancestor(10, parents, {30: "terminal", 40: "explorer"}) == "terminal", "not the nearest window"
    assert ccm.find_window_of_ancestor(10, parents, {10: "own"}) == "own", "own window skipped"
    assert ccm.find_window_of_ancestor(10, parents, {}) is None, "a window found where none is"
    assert ccm.find_window_of_ancestor(None, parents, {30: "terminal"}) is None, "a gone session found a window"
    assert ccm.find_window_of_ancestor(1, {1: 1}, {}) is None, "a parent loop never ends"


def test_session_pid_comes_from_its_session_file():
    ccm.SESSIONS_DIR = Path(tempfile.mkdtemp())
    (ccm.SESSIONS_DIR / "5.json").write_text(json.dumps({"pid": 5, "sessionId": "a"}), encoding="utf-8")
    (ccm.SESSIONS_DIR / "6.json").write_text("{half", encoding="utf-8")
    assert ccm.find_session_pid("a") == 5 and ccm.find_session_pid("b") is None


def test_only_own_page_posts_focus():
    focused = []
    ccm.focus_session_window = focused.append
    server = ccm.MonitorServer(("127.0.0.1", 0), ccm.MonitorHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for origin, status in (("http://evil.example", 403), (f"http://127.0.0.1:{port}", 204)):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/focus", body=json.dumps({"key": "a"}), headers={"Origin": origin})
            assert c.getresponse().status == status, origin
        assert focused == ["a"], focused
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
