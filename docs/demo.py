"""Draws the README GIFs docs/demo.gif and docs/demo-min.gif from fake sessions.

Each frame is the real page, served by ccm.MonitorServer over files in a temp folder, shot with Edge headless; ffmpeg joins the
frames into a 4 s loop. Needs Edge and ffmpeg. Run: python docs/demo.py
"""
import importlib.util
import json
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

DOCS = Path(__file__).resolve().parent
EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
FRAME_SECONDS = 0.5
FRAMES = 8  # 8 frames of 0.5 s: a 4 s loop
PILLS = ["5h:34%->17:00", "7d:21%->Mon 17:00"]

# Per session: name, model, effort, prompt, cache minutes left, ctx %, and per frame its status and progress line.
# A status "busy+agent" is busy with a working subagent row.
MAIN = [
    ("shop-api", "opus", "high", "/fix 32", 60, 20,
     ["busy", "busy+agent", "busy+agent", "busy+agent", "waiting", "waiting", "busy", "busy"],
     ["Step 1/4: reading the code"] * 2 + ["Step 2/4: running tests"] * 4 + ["Step 3/4: fixing the test"] * 2),
    ("docs-site", "sonnet", "medium", "add a dark theme", 57, 45,
     ["busy", "busy", "waiting", "waiting", "busy", "busy", "busy", "busy"],
     ["Step 2/3: editing styles"] * 4 + ["Step 3/3: checking the page"] * 4),
    ("infra", "opus", "high", "/review", 42, 39,
     ["waiting", "waiting", "busy", "busy", "busy", "busy", "waiting", "waiting"],
     ["Part 1/3: reading the diff"] * 2 + ["Part 2/3: checking the deploy"] * 4 + ["Part 3/3: writing findings"] * 2),
]
MINI = [
    ("shop-api", "opus", "high", "/fix 32", 60, 20, ["busy"] * 4 + ["busy+agent"] * 4,
     ["Step 1/4: reading the code"] * 4 + ["Step 2/4: running tests"] * 4),
    ("docs-site", "sonnet", "medium", "add a dark theme", 57, 45, ["busy"] * 8, ["Step 2/3: editing styles"] * 8),
    ("infra", "opus", "high", "/review", 42, 39, ["waiting"] * 4 + ["busy"] * 4,
     ["Part 1/3: reading the diff"] * 4 + ["Part 2/3: checking the deploy"] * 4),
    ("notes", "sonnet", "low", "sum up the week", 12, 8, ["idle"] * 8, [""] * 8),
]
TOOLS = [("Read", "file_path", "src/orders.py"), ("Grep", "pattern", "def total"), ("Bash", "command", "pytest -q tests"),
         ("Edit", "file_path", "src/orders.py")]


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        tmp = Path(folder)
        ccm = load_fake_monitor(tmp)
        server = ccm.MonitorServer(("127.0.0.1", 0), ccm.MonitorHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        try:
            draw_gif(ccm, tmp, url, MAIN, (838, 585), DOCS / "demo.gif")
            draw_gif(ccm, tmp, url, MINI, (480, 215), DOCS / "demo-min.gif")
        finally:
            server.shutdown()
            server.server_close()


def load_fake_monitor(tmp):
    """ccm.py with every folder it reads moved into tmp, as tests/test_page_view.py sets it up."""
    spec = importlib.util.spec_from_file_location("ccm", DOCS.parent / "ccm.py")
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    ccm.SESSIONS_DIR, ccm.PROJECTS_DIR = tmp / "sessions", tmp / "projects"
    ccm.CONFIG_PATH = tmp / "ccm" / "config.json"
    ccm.CLAUDE_DIR = ccm.PROGRESS_DIR = tmp
    ccm.global_parts = PILLS
    return ccm


def draw_gif(ccm, tmp, url, sessions, size, gif):
    frames = tmp / gif.stem
    frames.mkdir()
    now = datetime.now(timezone.utc)
    for frame in range(FRAMES):
        for index, session in enumerate(sessions):
            write_session(ccm, tmp, index, session, frame, now)
        shoot_frame(url, size, frames / f"{frame:02d}.png", tmp / "edge")
    clear_sessions(ccm)
    join_frames(frames, gif)
    print(f"Wrote {gif}")


def clear_sessions(ccm):
    """Removes every session file, transcript, progress note and token total, so the next GIF starts from no sessions."""
    shutil.rmtree(ccm.SESSIONS_DIR)
    shutil.rmtree(ccm.PROJECTS_DIR)
    for note in ccm.PROGRESS_DIR.glob("ccm-progress-*.txt"):
        note.unlink()
    ccm.transcript_totals.clear()


def write_session(ccm, tmp, index, session, frame, now):
    """The session file, transcript, subagent and progress note of one session as it stands in this frame."""
    name, model, effort, prompt, cache_minutes, ctx, states, notes = session
    session_id = f"{name}-id"
    status = states[frame].removesuffix("+agent")
    ccm.SESSIONS_DIR.mkdir(exist_ok=True)
    (ccm.SESSIONS_DIR / f"{session_id}.json").write_text(json.dumps(
        {"sessionId": session_id, "cwd": f"C:/src/{name}", "status": status, "updatedAt": time.time() * 1000 - index}))
    reply_time = stamp(now - timedelta(minutes=60 - cache_minutes))
    transcript = ccm.PROJECTS_DIR / name / f"{session_id}.jsonl"
    if not transcript.exists():
        transcript.parent.mkdir(parents=True)
        prompt_entry = {"type": "user", "origin": {"kind": "human"}, "timestamp": stamp(now - timedelta(seconds=40 + 7 * index)),
                        "message": {"content": prompt}}
        transcript.write_text(json.dumps(prompt_entry) + "\n")
    with transcript.open("a") as f:
        f.write(json.dumps(build_tool_call(model, effort, reply_time, frame + index, f"{name}-{frame}")) + "\n")
    write_subagent(transcript, states[frame].endswith("+agent"), reply_time, frame)
    note = tmp / f"ccm-progress-{session_id}.txt"
    if notes[frame]:
        note.write_text(notes[frame], encoding="utf-8")
    else:
        note.unlink(missing_ok=True)
    ccm.statuslines[session_id] = [f"~/{name}", "main", f"ctx {ctx}%"]


def write_subagent(transcript, working, reply_time, frame):
    sub = transcript.with_suffix("") / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "agent-a.meta.json").write_text(json.dumps({"agentType": "Explore", "finished": not working}))
    with (sub / "agent-a.jsonl").open("a") as f:
        f.write(json.dumps(build_tool_call("haiku", "low", reply_time, frame + 1, f"agent-{frame}")) + "\n")


def build_tool_call(model, effort, timestamp, turn, message_id):
    tool, key, value = TOOLS[turn % len(TOOLS)]
    return {"type": "assistant", "effort": effort, "timestamp": timestamp,
            "message": {"id": message_id, "model": model, "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "name": tool, "input": {key: value}}],
                        "usage": {"input_tokens": 6000, "output_tokens": 800}}}


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def shoot_frame(url, size, png, profile):
    """A screenshot at 2x pixels, taken at the page's load event; the page fits itself to the window before that."""
    width, height = size
    # No --virtual-time-budget: the page's /events stream never goes idle, so virtual time never ends and Edge never shoots.
    # Now and then the load event never comes either; --timeout=2000 then stops the load and shoots.
    # Edge's child processes inherit its handles, so its output goes to DEVNULL, not to a pipe that would stay open.
    subprocess.run([EDGE, "--headless", "--disable-gpu", "--no-first-run", "--hide-scrollbars", f"--user-data-dir={profile}",
                    f"--screenshot={png}", "--force-device-scale-factor=2", f"--window-size={width},{height}",
                    "--timeout=2000", url],
                   check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def join_frames(frames, gif):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(1 / FRAME_SECONDS), "-i", str(frames / "%02d.png"),
                    "-vf", "split[a][b];[a]palettegen[p];[b][p]paletteuse", "-loop", "0", str(gif)], check=True)


if __name__ == "__main__":
    main()
