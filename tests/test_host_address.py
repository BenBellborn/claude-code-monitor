import importlib.util
import socket
import threading
import urllib.request
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


def test_window_url_reaches_the_monitor_not_an_ipv6_listener():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    port = 8250  # outside 8080-8179
    other = socket.socket(socket.AF_INET6)
    other.bind(("::1", port))
    other.listen()

    def serve():
        try:
            c, _ = other.accept()
        except OSError:  # closed after the test, with nobody connected
            return
        c.recv(4096)
        c.sendall(b"HTTP/1.0 200 OK\r\n\r\nother app")
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    ccm.FIRST_PORT = port
    server = ccm.start_free_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        body = urllib.request.urlopen(f"http://{ccm.HOST}:{port}/ping", timeout=3).read()
    finally:
        server.shutdown()
        server.server_close()
        other.close()
    assert body == b"cc_monitor", "the free-port check and the window URL reach different sockets"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
