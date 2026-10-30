import http.client
import importlib.util
import json
import tempfile
import threading
import time
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
PROMPT = {"type": "user", "origin": {"kind": "human"}, "timestamp": "2026-10-29T10:00:00Z", "message": {"content": "/fix all"}}
CALL = {"type": "assistant", "effort": "high", "message": {"id": "m1", "model": "claude-opus-5-5", "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}], "usage": {"input_tokens": 1000, "output_tokens": 5}}}


def post_config(port, body, origin):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/config", body=json.dumps(body), headers={"Host": f"127.0.0.1:{port}", **({"Origin": origin} if origin else {})})
    return c.getresponse().status


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
    r = c.getresponse()
    if path == "/events":
        while not (line := r.fp.readline().decode()).startswith("data: "):
            pass
        c.close()
        return json.loads(line[6:])
    return r.read().decode()


def test_page_and_events_show_cards_pills_and_agent_rows():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR, ccm.PROJECTS_DIR = tmp / "sessions", tmp / "projects"
    ccm.CONFIG_PATH = tmp / "ccm" / "config.json"
    ccm.SESSIONS_DIR.mkdir()
    sub = ccm.PROJECTS_DIR / "p" / "s1" / "subagents"
    sub.mkdir(parents=True)
    (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps(
        {"sessionId": "s1", "cwd": "C:/x/proj", "status": "busy", "updatedAt": time.time() * 1000}))
    (ccm.PROJECTS_DIR / "p" / "s1.jsonl").write_text(json.dumps(PROMPT) + "\n" + json.dumps(CALL) + "\n")
    (sub / "agent-a.meta.json").write_text(json.dumps({"agentType": "verifier"}))
    (sub / "agent-a.jsonl").write_text(json.dumps(CALL) + "\n")
    ccm.statuslines["s1"] = ['<span class="red">main</span>']
    ccm.global_parts = ["5h:1%"]
    server = ccm.MonitorServer(("127.0.0.1", 0), ccm.MonitorHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = get(port, "/")
        assert "<title>\U0001f7e2 1</title>" in page, page
        assert "<h1>Claude Code Monitor</h1>" in page and f'"{ccm.VERSION}"' in page, "version label shown or reload check gone"
        assert 'pin.hidden = !("documentPictureInPicture" in window);' in page, "pin button not offered on every OS"
        assert '<b class="name">proj</b> - <span class="yellow">claude-opus-5-5</span>:<span class="yellow">high</span>' in page, page
        assert '<div class="state">Running: <span class="highlight">/fix all</span></div>' in page, page
        assert '<div class="progress">Grep: x · ' in page, page
        assert '<div class="mini">Working - <b class="name">proj</b> - Grep: x · ' in page, "one-line view of a running card"
        assert '- verifier - Grep: x</div>' in page, page
        assert '<div class="footer"><span class="red">main</span> | 1 tool calls | 1k in / 0k out</div>' in page, page
        assert '<p class="usage"><span class="pill">5h:1%</span></p>' in page, page
        assert "--working: #22c55e;" in page and json.loads(ccm.CONFIG_PATH.read_text()) == ccm.DEFAULT_CONFIG, "config file not created"
        assert post_config(port, {"min": True, "width": 500}, f"http://attacker.example:{port}") == 403, "post from another site"
        assert post_config(port, {"min": True, "width": 500, "height": "x"}, f"http://127.0.0.1:{port}") == 204
        config = json.loads(ccm.CONFIG_PATH.read_text())
        config["colors"]["working"] = "#123456"
        ccm.CONFIG_PATH.write_text(json.dumps(config))
        page = get(port, "/")
        assert 'windowMode = {"min": true, "width": 500, "height": 400}' in page, "window mode not saved or not served"
        assert "--working: #123456;" in page, "edited color not served"
        ccm.CONFIG_PATH.write_text("{ broken")
        post_config(port, {"min": False}, f"http://127.0.0.1:{port}")
        assert ccm.CONFIG_PATH.read_text() == "{ broken", "config being edited overwritten"
        (sub / "agent-a.meta.json").write_text(json.dumps({"agentType": "verifier", "finished": True}))
        version, title, cards = get(port, "/events")
        assert (version, title) == (ccm.VERSION, "\U0001f7e2 1"), (version, title)
        assert "verifier" not in cards, f"finished agent still shown: {cards}"
        (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps(
            {"sessionId": "s1", "cwd": "C:/x/proj", "status": "idle", "updatedAt": time.time() * 1000}))
        cards = get(port, "/events")[2]
        assert '<div class="agent">' not in cards, f"idle card with no agents shows an agent row: {cards}"
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
