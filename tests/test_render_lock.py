import importlib.util
import json
import tempfile
import threading
import time
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


def prompt(text):
    return {"type": "user", "origin": {"kind": "human"}, "timestamp": "2026-10-29T10:00:00Z", "message": {"content": text}}


def call(message_id):
    return {"type": "assistant", "message": {"id": message_id, "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                                             "usage": {"input_tokens": 100, "output_tokens": 10}}}


def test_concurrent_renders_scan_each_block_once():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    root = Path(tempfile.mkdtemp())
    ccm.SESSIONS_DIR = root / "sessions"
    ccm.PROJECTS_DIR = root / "projects"
    ccm.SESSIONS_DIR.mkdir()
    (ccm.PROJECTS_DIR / "p").mkdir(parents=True)
    (ccm.SESSIONS_DIR / "s.json").write_text(json.dumps(
        {"sessionId": "s1", "updatedAt": time.time() * 1000, "status": "busy", "cwd": str(root)}), encoding="utf-8")
    transcript = ccm.PROJECTS_DIR / "p" / "s1.jsonl"

    def append(*entries):
        with transcript.open("ab") as f:
            f.write("".join(json.dumps(e) + "\n" for e in entries).encode())

    append(prompt("first"))
    ccm.update_sessions()
    append(call("m1"))
    real_open = open

    class SlowRead:  # holds each scan between its read and its offset advance
        def __init__(self, f): self.f = f
        def __enter__(self): return self
        def __exit__(self, *a): self.f.close()
        def __getattr__(self, name): return getattr(self.f, name)
        def read(self, *a):
            data = self.f.read(*a)
            time.sleep(0.3)
            return data

    ccm.open = lambda p, m: SlowRead(real_open(p, m))
    errors = []

    def render():
        try:
            ccm.update_sessions()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=render) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    del ccm.open
    append(prompt("second"))
    ccm.update_sessions()
    totals = ccm.transcript_totals[transcript]
    assert totals["offset"] == transcript.stat().st_size, (totals["offset"], transcript.stat().st_size)
    assert totals["turn_prompt"] == "second", "second prompt skipped: offset advanced twice"
    assert totals["in"] == 100, "token totals counted twice"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
