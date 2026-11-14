import http.client
import importlib.util
import tempfile
import threading
import time
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


def get(port, path, host):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Host": host})
    r = c.getresponse()
    return r.status, r.read().decode()


def test_only_own_host_reads_the_page():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    ccm.update_sessions = lambda: []
    ccm.CONFIG_PATH = Path(tempfile.mkdtemp()) / "config.json"
    ccm.build_title_and_cards = lambda sessions: ("T", "<b>SECRET-PROMPT</b>")
    server = ccm.MonitorServer(("127.0.0.1", 0), ccm.MonitorHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for path in ("/", "/ping", "/events"):
            status, body = get(port, path, f"rebind.attacker.example:{port}")
            assert status == 403 and "SECRET-PROMPT" not in body and "cc_monitor" not in body, (path, status)
        status, _ = get(port, "/", f"localhost:{port + 1}")
        assert status == 403, ("other port", status)
        for host in (f"localhost:{port}", f"127.0.0.1:{port}"):
            status, body = get(port, "/", host)
            assert status == 200 and "SECRET-PROMPT" in body, (host, status)
            assert get(port, "/ping", host) == (200, "cc_monitor"), host
    finally:
        server.shutdown()
        server.server_close()


def test_foreign_post_gets_its_403_when_its_body_comes_late():
    # A body that reaches an already closed socket makes Windows send a reset, which drops the 403 not yet read.
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    server = ccm.MonitorServer(("127.0.0.1", 0), ccm.MonitorHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.putrequest("POST", "/config", skip_host=True)
        for name, value in (("Host", f"127.0.0.1:{port}"), ("Origin", f"http://attacker.example:{port}"), ("Content-Length", "14")):
            c.putheader(name, value)
        c.endheaders()
        time.sleep(0.2)
        c.send(b'{"width": 500}')
        time.sleep(0.2)
        assert c.getresponse().status == 403
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/config", body=b"[" * 5000, headers={"Host": f"127.0.0.1:{port}", "Origin": f"http://attacker.example:{port}"})
        assert c.getresponse().status == 403, "a body nested too deep to parse dropped the 403"
    finally:
        server.shutdown()
        server.server_close()


def test_debug_post_without_line_gets_400():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    ccm.CACHE = Path(tempfile.mkdtemp())
    server = ccm.MonitorServer(("127.0.0.1", 0), ccm.MonitorHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/debug", body=b"{}", headers={"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"})
        assert c.getresponse().status == 400
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
