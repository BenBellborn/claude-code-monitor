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


def test_page_and_events_show_cards_pills_and_padded_agent_rows():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR, ccm.PROJECTS_DIR = tmp / "sessions", tmp / "projects"
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
        assert list(ccm.recent_titles) == ["\U0001f7e2 1"], ccm.recent_titles
        assert '<b class="name">proj</b> - <span class="yellow">claude-opus-5-5</span>:<span class="yellow">high</span>' in page, page
        assert '<div class="state">Running: <span class="highlight">/fix all</span></div>' in page, page
        assert '<div class="progress">Grep: x · ' in page, page
        assert '- verifier - Grep: x</div>' in page, page
        assert '<div class="footer"><span class="red">main</span> | 1 tool calls | 1k in / 0k out</div>' in page, page
        assert '<p class="usage"><span class="pill">5h:1%</span></p>' in page, page
        (sub / "agent-a.meta.json").write_text(json.dumps({"agentType": "verifier", "finished": True}))
        version, title, cards = get(port, "/events")
        assert (version, title) == (ccm.VERSION, "\U0001f7e2 1"), (version, title)
        assert "verifier" not in cards and '<div class="agent">&nbsp;</div>' in cards, f"agent row not padded: {cards}"
        (ccm.SESSIONS_DIR / "1.json").write_text(json.dumps(
            {"sessionId": "s1", "cwd": "C:/x/proj", "status": "idle", "updatedAt": time.time() * 1000}))
        cards = get(port, "/events")[2]
        assert '<div class="agent">' not in cards, f"idle card keeps its padded agent row: {cards}"
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
