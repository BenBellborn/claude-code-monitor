import ctypes
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import date, datetime
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
FIRST_PORT = 8080
# Chromium browsers that open a page as its own app window; the first one found is used, else a tab in the default browser.
APP_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "chromium", "chromium-browser", "microsoft-edge",
]
# A child that outlives this process and the terminal or hook that started it, with no console window.
DETACHED = {
    "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
    **({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
       else {"start_new_session": True}),
}
# An idle card fades once its session has been idle this long.
IDLE_FADE_SECONDS = 10 * 60
SCRIPT = Path(__file__).resolve()
CACHE = Path.home() / ".claude" / "cache"
# The script's modified time at start: the page reloads when the server it talks to has another one.
VERSION = str(SCRIPT.stat().st_mtime_ns)
# Bytes read from the end of a transcript; the last model, effort and prompt sit near the end.
TAIL_BYTES = 512 * 1024
# Longest prompt or activity text shown, so a row stays on one line.
# Tool arguments that best name what a call does, in order of preference (same as the agent-models mod).
ACTIVITY_KEYS = ["description", "file_path", "pattern", "command", "url", "query", "prompt"]
SILENT_TOOLS = {"SubagentHandback"}
GENERIC_TYPES = {"general-purpose"}
# A path starts a token and is rooted or has 2+ folders, so "and/or" stays.
PATH = re.compile(r"""(?<![^\s"'=(])(?:(?:[A-Za-z]:|~|\.{1,2})?[\\/](?:[^\s\\/"'`]+[\\/])*|(?:[^\s\\/"'`]+[\\/]){2,})([^\s\\/"'`]*)""")


def read_tail(path):
    """Parsed JSON lines from the end of a transcript, oldest first."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    if size > TAIL_BYTES:
        lines = lines[1:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    return entries


def shorten(text):
    text = PATH.sub(lambda m: m.group(1), text.split("\n")[0])
    return text


def find_model_effort(entries):
    for e in reversed(entries):
        if e.get("type") == "assistant":
            return e["message"].get("model", "?"), e.get("effort", "?")
    return "?", "?"


def find_activity(entries):
    """The subagent's last tool call, as the agent-models mod shows it."""
    for e in reversed(entries):
        if e.get("type") != "assistant":
            continue
        for block in reversed(e["message"].get("content", [])):
            if block.get("type") != "tool_use" or block.get("name") in SILENT_TOOLS:
                continue
            args = block.get("input", {})
            key = next((k for k in ACTIVITY_KEYS if isinstance(args.get(k), str) and args[k]), None)
            return shorten(f"{block['name']}: {args[key]}") if key else block["name"]
    return None


def find_running_task(totals):
    """The skill the current turn runs, else its prompt."""
    if totals["turn_skill"]:
        return "/" + totals["turn_skill"]
    prompt = totals["turn_prompt"]
    # A slash command is kept whole: shortening would read "/fix" as a path.
    if prompt.startswith("/"):
        return prompt.split("\n")[0]
    return shorten(prompt)


def read_prompt_text(entry):
    """The prompt as typed; a slash command is stored as tags and comes back as "/fix all"."""
    content = entry["message"].get("content")
    text = content if isinstance(content, str) else "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    command = re.search(r"<command-name>(.*?)</command-name>", text)
    if command:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        return f"{command.group(1)} {args.group(1) if args else ''}".strip()
    return text.strip()


def is_prompt(entry):
    """A prompt or slash command the user typed; compaction summaries, task notifications, interrupts and tool results are not."""
    return entry.get("type") == "user" and (entry.get("origin") or {}).get("kind") == "human"


def find_turn_progress(entries, totals):
    """Current tool call, elapsed time and tool call count of the turn since the last prompt."""
    activity = find_activity(entries)
    progress = [activity] if activity else []
    if totals["turn_start"]:
        begun = datetime.fromisoformat(totals["turn_start"].replace("Z", "+00:00"))
        elapsed = int((datetime.now(begun.tzinfo) - begun).total_seconds())
        progress.append(f"{elapsed // 60}m {elapsed % 60:02d}s")
    return progress


def format_tokens(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def find_context_tokens(entries):
    for e in reversed(entries):
        if e.get("type") == "assistant":
            u = e["message"].get("usage", {})
            return u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
    return 0


# Per transcript: bytes read so far, message ids counted, token totals, and the current turn's start and tool calls.
transcript_totals = {}


def scan_transcript(transcript):
    """Session token totals and current turn counters, reading only what was appended since the last call."""
    totals = transcript_totals.setdefault(
        transcript,
        {"offset": 0, "seen": set(), "in": 0, "out": 0, "turn_start": None, "turn_calls": 0, "turn_prompt": "", "turn_skill": None},
    )
    with open(transcript, "rb") as f:
        f.seek(totals["offset"])
        data = f.read()
    # A line still being written stays for the next call.
    complete = data[: data.rfind(b"\n") + 1]
    totals["offset"] += len(complete)
    for line in complete.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if is_prompt(e):
            totals.update(turn_start=e.get("timestamp"), turn_calls=0, turn_prompt=read_prompt_text(e), turn_skill=None)
        message = e.get("message") if e.get("type") == "assistant" else None
        if not message:
            continue
        if e.get("attributionSkill"):
            totals["turn_skill"] = e["attributionSkill"]
        totals["turn_calls"] += sum(b.get("type") == "tool_use" for b in message.get("content", []))
        # A reply with several content blocks is written once per block, each with the same usage.
        if message.get("id") in totals["seen"]:
            continue
        totals["seen"].add(message.get("id"))
        u = message.get("usage", {})
        totals["in"] += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        totals["out"] += u.get("output_tokens", 0)
    return totals


def get_active_subagents(transcript):
    agents = []
    for meta_path in sorted(transcript.with_suffix("").glob("subagents/*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("finished"):
            continue
        entries = read_tail(meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl")))
        model, effort = find_model_effort(entries)
        agent_type = meta.get("agentType", "")
        agents.append({
            "model": model,
            "effort": effort,
            "type": "" if agent_type in GENERIC_TYPES else agent_type,
            "activity": find_activity(entries) or meta.get("description", ""),
        })
    return agents


def get_sessions():
    today = date.today()
    sessions = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if datetime.fromtimestamp(s.get("updatedAt", 0) / 1000).date() != today:
            continue
        # A spare is a background session started ahead of a job; it has no prompt or transcript yet.
        if s.get("spare"):
            continue
        transcript = next(PROJECTS_DIR.glob(f"*/{s['sessionId']}.jsonl"), None)
        entries = read_tail(transcript) if transcript else []
        model, effort = find_model_effort(entries)
        status = s.get("status")
        progress, footer = [], []
        if transcript:
            totals = scan_transcript(transcript)
            if status == "busy":
                progress = find_turn_progress(entries, totals)
                footer.append(f"{totals['turn_calls']} tool calls")
            footer.append(f"{format_tokens(totals['in'])} in / {format_tokens(totals['out'])} out")
            statusline_inputs[s["sessionId"]] = {
                "session_id": s["sessionId"],
                "transcript_path": str(transcript),
                "cwd": s.get("cwd", ""),
                "model": model,
                "context_tokens": find_context_tokens(entries),
            }
        task = find_running_task(totals) if transcript else ""
        detail, highlight = task, bool(task)
        color = {"busy": "working", "waiting": "waiting"}.get(status, "idle")
        sessions.append({
            "key": s["sessionId"],
            "cwd": s.get("cwd", ""),
            # A copied session is named "ARC (2)": the number is dropped so it matches its original.
            "name": re.sub(r"\s*\(\d+\)$", "", s.get("name") or Path(s.get("cwd", "")).name),
            "updated": s.get("statusUpdatedAt") or s.get("updatedAt", 0),
            "model": model,
            "effort": str(effort),
            "detail": detail,
            "highlight": highlight,
            "color": color,
            "progress": " · ".join(progress),
            "footer": footer,
            "agents": get_active_subagents(transcript) if transcript else [],
        })
    return sorted(label_sessions(sessions), key=lambda s: s["cwd"].lower())


def label_sessions(sessions):
    """Keeps one session per folder and name, and prefixes the parent folder where a name clashes across folders."""
    # An idle session loses to a busy or blocked one; between equals the most recently active one is kept.
    rank = lambda s: (s["color"] != "idle", s["updated"])
    newest = {}
    for s in sessions:
        key = (s["cwd"].lower(), s["name"])
        if key not in newest or rank(s) > rank(newest[key]):
            newest[key] = s
    kept = list(newest.values())
    for s in kept:
        folder = Path(s["cwd"])
        clashes = [o for o in kept if o is not s and (o["name"] == s["name"] or Path(o["cwd"]).name == folder.name)]
        if any(o["cwd"].lower() != s["cwd"].lower() for o in clashes):
            s["label"] = f"{folder.parent.name}-{s['name']}"
        else:
            s["label"] = s["name"]
    return kept


def build_title(sessions):
    """Tab title: running sessions and ones waiting on the user (blocked or idle), leaving out a count of none."""
    running = sum(s["color"] == "working" for s in sessions)
    counts = [("🟢", running), ("⛔", len(sessions) - running)]
    return " ".join(f"{symbol} {n}" for symbol, n in counts if n) or "No Session"


# Colors of the agent-models mod: its 'claude' theme key is orange, 'warning' yellow, 'inactive' grey.
def model_color(model):
    return "orange" if "fable" in model else "yellow" if "opus" in model else "grey"


def effort_color(effort):
    return "orange" if effort in ("max", "xhigh", "ultra") else "yellow" if effort == "high" else "grey"


def render_model_effort(model, effort):
    return (
        f'<span class="{model_color(model)}">{html.escape(model)}</span>:'
        f'<span class="{effort_color(effort)}">{html.escape(str(effort))}</span>'
    )


# statusLine runs: the input per session, its parts per session, and the parts every session shares (rate limits, tool gains).
statusline_inputs = {}
statuslines = {}
global_parts = []
GLOBAL_PART = re.compile(r"^(5h|7d):|\b(rtk|codemap) ")
ANSI = re.compile(r"\x1b\[([\d;]*)m")
STATUSLINE_CACHE = CACHE / "cc-monitor-statuslines.json"
ANSI_CLASSES = {"31": "red", "33": "yellow", "38;5;208": "orange"}


def find_git_bash():
    """The bash Claude Code runs statusline commands with; the command's ~ paths need it."""
    configured = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if configured:
        return configured
    # git.exe sits in Git\cmd or Git\mingw64\bin depending on PATH; bash.exe is in Git\bin.
    git = shutil.which("git")
    for folder in Path(git).resolve().parents if git else []:
        if (folder / "bin" / "bash.exe").exists():
            return str(folder / "bin" / "bash.exe")
    return "bash"


def find_context_window(model):
    for path in (CACHE / "model-catalog").glob("*.json"):
        try:
            models = json.loads(path.read_text(encoding="utf-8"))["catalog"]["config"]["models"]
            size = next((m["runtime"]["max_input_tokens"] for m in models if m.get("id") == model), None)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if isinstance(size, int) and size > 0:
            return size
    return 200000


def build_statusline_input(info, limits):
    """The statusLine JSON for a session; model and effort are left out, so the command skips those parts."""
    window = find_context_window(info["model"])
    data = {
        "session_id": info["session_id"],
        "transcript_path": info["transcript_path"],
        "cwd": info["cwd"],
        "workspace": {"current_dir": info["cwd"], "project_dir": info["cwd"]},
        "context_window": {"context_window_size": window, "used_percentage": 100 * info["context_tokens"] / window},
    }
    if limits:
        data["rate_limits"] = limits
    return data


def render_ansi(text):
    """Statusline text with its ANSI colours as spans; a reset or unknown code ends the colour."""
    out, color = [], ""
    for i, piece in enumerate(ANSI.split(text)):
        if i % 2:
            color = ANSI_CLASSES.get(piece, "")
        elif piece:
            out.append(f'<span class="{color}">{html.escape(piece)}</span>' if color else html.escape(piece))
    return "".join(out)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_statuslines():
    """Runs the settings.json statusLine command for every session every 10 s and stores its parts."""
    global global_parts
    bash = find_git_bash()
    # The last lines from before a restart show until the first run replaces them.
    saved = read_json(STATUSLINE_CACHE)
    is_text_list = lambda v: isinstance(v, list) and all(isinstance(p, str) for p in v)
    if isinstance(saved, dict) and isinstance(saved.get("sessions"), dict):
        statuslines.update({k: v for k, v in saved["sessions"].items() if is_text_list(v)})
    if isinstance(saved, dict) and is_text_list(saved.get("shared")):
        global_parts = saved["shared"]
    while True:
        settings = read_json(CLAUDE_DIR / "settings.json")
        status_line = settings.get("statusLine") if isinstance(settings, dict) else None
        command = status_line.get("command") if isinstance(status_line, dict) else None
        limits = read_json(CACHE / "rate-limits.json")
        # ponytail: one run per session every 10 s (jq and git forks each); per render would start ~15 processes per second.
        for key, info in list(statusline_inputs.items()) if isinstance(command, str) and command.strip() else []:
            # The command and its output are the user's; a run that fails in any way keeps the last line: stale beats empty.
            try:
                out = subprocess.run(
                    [bash, "-c", command], input=json.dumps(build_statusline_input(info, limits if isinstance(limits, dict) else None)),
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).stdout or ""
                parts = [p for p in out.strip().split(" | ") if p.strip()]
                shared = [render_ansi(p) for p in parts if GLOBAL_PART.search(ANSI.sub("", p))]
                own = [render_ansi(p) for p in parts if not GLOBAL_PART.search(ANSI.sub("", p))]
            except Exception:
                continue
            if own:
                statuslines[key] = own
            if shared:
                global_parts = shared
        try:
            STATUSLINE_CACHE.write_text(json.dumps({"sessions": statuslines, "shared": global_parts}), encoding="utf-8")
        except OSError:
            pass
        time.sleep(10)


def render_usage():
    """The statusline parts every session shares, as pills under the cards."""
    return '<p class="usage">' + "".join(f'<span class="pill">{p}</span>' for p in global_parts) + '</p>' if global_parts else ""


# Most agent rows each card has shown; cards pad to it so they grow but never shrink until the script restarts.
max_agent_rows = {}
# Scans and render state are read-modify-written; every request thread renders.
render_lock = threading.Lock()


def render_sessions():
    """The tab title and the session cards' HTML."""
    with render_lock:
        sessions = get_sessions()
        cards = []
        for s in sessions:
            rows = [
                f'<div class="agent">● {render_model_effort(a["model"], a["effort"])} - '
                f'{html.escape(a["type"]) + " - " if a["type"] else ""}{html.escape(a["activity"])}</div>'
                for a in s["agents"]
            ]
            max_agent_rows[s["key"]] = max(max_agent_rows.get(s["key"], 0), len(rows))
            rows += ['<div class="agent">&nbsp;</div>'] * (max_agent_rows[s["key"]] - len(rows))
            agents = "".join(rows)
            detail = html.escape(s["detail"])
            if s["highlight"]:
                detail = f'<span class="highlight">{detail}</span>'
            faded = " faded" if s["color"] == "idle" and time.time() - s["updated"] / 1000 > IDLE_FADE_SECONDS else ""
            cards.append(
                f'<div class="card {s["color"]}{faded}" data-key="{html.escape(s["key"])}">'
                f'<b class="name">{html.escape(s["label"])}</b> - {render_model_effort(s["model"], s["effort"])}'
                f'<div class="state">Running: {detail}</div>'
                f'<div class="progress">{STOPPED_LINES.get(s["color"]) or html.escape(s["progress"]) or "&nbsp;"}</div>{agents}'
                f'<div class="footer">{" | ".join(statuslines.get(s["key"], []) + [html.escape(f) for f in s["footer"]]) or "&nbsp;"}</div></div>'
            )
        title = build_title(sessions)
        if not recent_titles or recent_titles[-1] != title:
            recent_titles.append(title)
        return title, ("".join(cards) or '<p class="empty">No sessions today.</p>') + render_usage()


STOPPED_LINES = {
    "waiting": '<span class="stopped">Blocked</span> Awaiting user input',
    "idle": '<span class="stopped">Idle</span>',
}


PAGE = """<!DOCTYPE html>
<html>
<head>
    <title>__TITLE__</title>
    <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='12' fill='%23111'/%3E%3Cpolyline points='8,34 20,34 26,18 34,48 40,28 44,34 56,34' fill='none' stroke='%234ade80' stroke-width='5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
    <style>
        body { font-family: -apple-system, sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; max-width: 900px; margin: auto; }
        .card { background: #1e293b; padding: 1rem 15px 1rem 1rem; border-radius: 6px; margin-bottom: 0.75rem; border-left: 4px solid #64748b; box-sizing: border-box; }
        .working { border-left-color: #22c55e; }
        .waiting { border-left-color: #f59e0b; }
        .idle { border-left-color: #475569; }
        .faded { opacity: 0.7; }
        .state { color: #94a3b8; margin-top: 0.25rem; }
        .progress, .footer { color: #94a3b8; font-size: 0.85rem; margin-top: 0.15rem; }
        .agent { color: #cbd5e1; font-size: 0.85rem; margin: 0.25rem 0 0 1rem; }
        /* One line each, never wrapped or cut short; the card edge clips what does not fit. */
        .state, .progress, .agent, .footer { white-space: nowrap; overflow: hidden; }
        .version { color: #64748b; font-size: 0.9rem; font-weight: normal; }
        .empty, .note { color: #64748b; }
        .usage { color: #cbd5e1; display: flex; flex-wrap: wrap; gap: 0.5rem; }
        .pill { background: #1e293b; padding: 2px 10px; border-radius: 999px; }
        .name { color: #ffffff; }
        .orange { color: #d97757; }
        .yellow { color: #eab308; }
        .grey { color: #94a3b8; }
        .red { color: #ef4444; }
        .stopped { color: #ffffff; background: #dc2626; padding: 0 4px; border-radius: 3px; font-weight: 600; }
        .highlight { color: #0f172a; background: #fbbf24; padding: 0 4px; border-radius: 3px; font-weight: 600; }
    </style>
</head>
<body>
    <h1>Claude Code Monitor <span class="version">version: __VERSION_TIME__</span></h1>
    <div id="sessions">__SESSIONS__</div>
    <p class="note" id="note"></p>
    <script>
        // A card never shrinks: each keeps the tallest height it has had.
        const maxHeights = {};
        function lockCardHeights() {
            for (const card of document.querySelectorAll(".card[data-key]")) {
                card.style.minHeight = "";
                const key = card.dataset.key;
                maxHeights[key] = Math.max(maxHeights[key] || 0, card.offsetHeight);
                card.style.minHeight = maxHeights[key] + "px";
            }
        }
        lockCardHeights();
        // A reload while the monitor restarts would leave an error page with no script to recover; reload only once the page loads.
        async function reloadWhenUp() {
            for (;;) {
                try {
                    if ((await fetch(location.href, { cache: "no-store" })).ok) { location.reload(); return; }
                } catch { }
                await new Promise(done => setTimeout(done, 1000));
            }
        }
        const events = new EventSource("/events");
        events.onmessage = e => {
            const [version, title, cards] = JSON.parse(e.data);
            if (version !== "__VERSION__") { reloadWhenUp(); return; }
            document.title = title;
            document.getElementById("sessions").innerHTML = cards;
            lockCardHeights();
        };
        let scanning = false;
        // A restarted monitor may bind another port; move to the first one that answers.
        async function findMonitor() {
            if (scanning) return;
            scanning = true;
            while (events.readyState !== EventSource.OPEN) {
                for (let port = __FIRST_PORT__; port < __FIRST_PORT__ + 100 && events.readyState !== EventSource.OPEN; port++) {
                    try {
                        const r = await fetch(`http://${location.hostname}:${port}/ping`, { signal: AbortSignal.timeout(300) });
                        if (await r.text() !== "cc_monitor") continue;
                        if (String(port) !== location.port) location.href = `http://${location.hostname}:${port}/`;
                        break;
                    } catch { }
                }
                await new Promise(done => setTimeout(done, 2000));
            }
            scanning = false;
        }
        events.onerror = () => { document.getElementById("note").textContent = "Disconnected, retrying…"; findMonitor(); };
        events.onopen = () => { document.getElementById("note").textContent = ""; };
    </script>
</body>
</html>
"""


class MonitorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # A foreign Host is a DNS-rebinding page reaching 127.0.0.1; it must not read the sessions.
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"localhost:{port}", f"127.0.0.1:{port}"):
            self.send_error(403)
            return
        if self.path == "/events":
            self.stream_events()
            return
        if self.path == "/ping":
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"cc_monitor")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        title, cards = render_sessions()
        version_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(VERSION) / 1e9))
        page = PAGE.replace("__VERSION_TIME__", version_time).replace("__VERSION__", VERSION).replace("__FIRST_PORT__", str(FIRST_PORT)).replace("__TITLE__", title).replace("__SESSIONS__", cards)
        self.wfile.write(page.encode("utf-8"))

    def stream_events(self):
        """Sends the session list whenever it changes, until the browser disconnects."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        global missed_heartbeats
        last = None
        # ponytail: re-reads the session files every second; a file watcher if that ever costs too much.
        try:
            while True:
                current = [VERSION, *render_sessions()]
                # An unchanged list still sends a comment line, so a closed window shows up as a failed write.
                self.wfile.write(f"data: {json.dumps(current)}\n\n".encode("utf-8") if current != last else b":\n\n")
                self.wfile.flush()
                last = current
                missed_heartbeats = 0
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, format, *args):
        return  # Suppress console log spam


def restart_on_new_version(port):
    """Starts the edited script on the same port in a new process and exits this one; an edit that does not compile keeps this one running."""
    seen = VERSION
    while True:
        time.sleep(1)
        current = str(SCRIPT.stat().st_mtime_ns)
        if current == seen:
            continue
        seen = current
        try:
            compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec")
        except SyntaxError as e:
            print(f"New version not loaded: {e}")
            continue
        print("New version found, restarting.")
        subprocess.Popen([sys.executable, str(SCRIPT), "--reloaded", str(port)])
        os._exit(0)


# Counted in 1 s ticks, not wall-clock time: a clock jump (resume from sleep, NTP step) must not end a viewed monitor.
missed_heartbeats = 0
NO_VIEWER_EXIT_SECONDS = 20


def exit_when_window_closed():
    """Closing the window stops the monitor, so the next session start opens a new one."""
    global missed_heartbeats
    while True:
        time.sleep(1)
        missed_heartbeats += 1
        if missed_heartbeats > NO_VIEWER_EXIT_SECONDS:
            os._exit(0)


# The last titles sent; the window still shows the previous one for up to a second after a change.
recent_titles = deque(maxlen=5)


def open_monitor_window(port):
    url = f"http://localhost:{port}/"
    browser = next((b for b in APP_BROWSERS if Path(b).is_file() or (os.sep not in b and shutil.which(b))), None)
    if not browser:
        webbrowser.open_new_tab(url)
        return
    # App mode: its own window without tabs or address bar, like an HTA.
    subprocess.Popen([browser, f"--app={url}", "--window-size=920,700"], **DETACHED)


def pin_monitor_window(port):
    """Keeps the monitor's app window above all other windows, and replaces it when it is stuck on a load error page."""
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    topmost = wintypes.HWND(-1)
    no_move_size_activate = 0x0001 | 0x0002 | 0x0010
    wm_close = 0x0010
    # An app window whose page failed to load has the bare host as its title; that page has no script to retry.
    error_title = f"localhost:{port}"
    error_checks = {}

    def pin(hwnd, _):
        buffer = ctypes.create_unicode_buffer(user32.GetWindowTextLengthW(hwnd) + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        if not user32.IsWindowVisible(hwnd):
            return True
        # Exact title: an Edge browser window with the monitor in a tab adds " - Microsoft Edge" and stays unpinned.
        if buffer.value in recent_titles:
            user32.SetWindowPos(hwnd, topmost, 0, 0, 0, 0, no_move_size_activate)
        if buffer.value == error_title:
            seen_error[hwnd] = error_checks.get(hwnd, 0) + 1
        return True

    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(pin)
    while True:
        seen_error = {}
        user32.EnumWindows(callback, 0)
        error_checks = seen_error
        # Two checks in a row (3-6 s) on the error page: a page load mid-restart would have recovered by now.
        for hwnd in [h for h, n in error_checks.items() if n >= 2]:
            user32.PostMessageW(hwnd, wm_close, 0, 0)
            del error_checks[hwnd]
            open_monitor_window(port)
        time.sleep(3)


class MonitorServer(ThreadingHTTPServer):
    # Without this, Windows lets a second server bind a port already in use, so a taken port would never be skipped.
    allow_reuse_address = False
    daemon_threads = True


def start_free_server():
    """Binds the first free port from FIRST_PORT on."""
    for port in range(FIRST_PORT, FIRST_PORT + 100):
        try:
            return MonitorServer(("127.0.0.1", port), MonitorHandler)
        except OSError:
            continue
    raise OSError(f"No free port in {FIRST_PORT}-{FIRST_PORT + 99}")


def start_reloaded_server(port):
    """Binds the port the open window uses, waiting for the older process to let it go."""
    for _ in range(40):
        try:
            return MonitorServer(("127.0.0.1", port), MonitorHandler)
        except OSError:
            time.sleep(0.25)
    return MonitorServer(("127.0.0.1", port), MonitorHandler)


def claim_single_instance(reloaded):
    """True when no other monitor runs; a session start hook runs this on every session. The claim lives as long as the
    process. A restart after an edit takes it over from the process it replaces. A monitor whose window just closed holds
    the claim until it exits, so a session started in that time waits for it instead of leaving no monitor."""
    for _ in range(NO_VIEWER_EXIT_SECONDS + 5):
        if try_claim_instance(reloaded):
            return True
        time.sleep(1)
    return False


def try_claim_instance(reloaded):
    global instance_lock
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, "Local\\cc_monitor")
        if reloaded or ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            return True
        # An open handle keeps the mutex alive after the other monitor exits.
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    import fcntl
    instance_lock = open(CACHE / "ccm.lock", "w")
    try:
        # The replaced process exits right after starting this one, so a restart waits for its lock.
        fcntl.flock(instance_lock, fcntl.LOCK_EX if reloaded else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        instance_lock.close()
        return False
    return True


STARTED_SESSIONS = CACHE / "ccm-started-sessions.txt"


def claim_hook_start():
    """False for a prompt hook in a session the monitor already started for, so a closed window stays closed until the
    next session. No hook fires on plugin install; the first prompt after it starts the monitor."""
    event = json.loads(sys.stdin.read() or "{}")
    session = str(event.get("session_id", ""))
    started = STARTED_SESSIONS.read_text().split() if STARTED_SESSIONS.exists() else []
    if session in started:
        return event.get("hook_event_name") != "UserPromptSubmit"
    CACHE.mkdir(parents=True, exist_ok=True)
    STARTED_SESSIONS.write_text("\n".join((started + [session])[-200:]))
    return True


if __name__ == "__main__":
    # Start again detached and return, so the session start hook and `! python ccm.py` in Claude Code return at once and
    # the monitor outlives them. On Windows it runs as pythonw.exe, without a console window.
    if os.environ.get("CCM_DETACHED") != "1":
        if "--hook" in sys.argv and not claim_hook_start():
            sys.exit(0)
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw) if sys.platform == "win32" and pythonw.exists() else sys.executable
        subprocess.Popen([exe, str(SCRIPT), *sys.argv[1:]], env={**os.environ, "CCM_DETACHED": "1"}, **DETACHED)
        sys.exit(0)
    CACHE.mkdir(parents=True, exist_ok=True)
    # A restart after an edit keeps the port and the window that is already open; the page reloads itself.
    reloaded = "--reloaded" in sys.argv
    if not claim_single_instance(reloaded):
        sys.exit(0)
    server = start_reloaded_server(int(sys.argv[sys.argv.index("--reloaded") + 1])) if reloaded else start_free_server()
    port = server.server_address[1]
    print(f"Claude Monitor running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    if not reloaded:
        open_monitor_window(port)
    threading.Thread(target=restart_on_new_version, args=(port,), daemon=True).start()
    threading.Thread(target=run_statuslines, daemon=True).start()
    threading.Thread(target=exit_when_window_closed, daemon=True).start()
    if sys.platform == "win32":
        threading.Thread(target=pin_monitor_window, args=(port,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
