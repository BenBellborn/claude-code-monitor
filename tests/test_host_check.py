import http.client
import importlib.util
import threading
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
