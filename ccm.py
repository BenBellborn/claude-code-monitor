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
from types import SimpleNamespace
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

CLAUDE_DIR = Path.home() / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
FIRST_PORT = 8080
# The window URL must name the address the server binds: "localhost" may resolve to ::1 first and reach another app there.
HOST = "127.0.0.1"
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
# A subagent stopped or killed mid-call is never marked finished; one silent this long is gone. A tool call times out at 10 minutes.
SUBAGENT_GONE_SECONDS = 15 * 60
SCRIPT = Path(__file__).resolve()
CACHE = Path.home() / ".claude" / "cache"
# The script's modified time at start: the page reloads when the server it talks to has another one.
VERSION = str(SCRIPT.stat().st_mtime_ns)
# Tool arguments that best name what a call does, in order of preference (same as the agent-models mod).
ACTIVITY_KEYS = ["description", "file_path", "pattern", "command", "url", "query", "prompt"]
SILENT_TOOLS = {"SubagentHandback"}
GENERIC_TYPES = {"general-purpose"}
# A path starts a token and is rooted or has 2+ folders, so "and/or" stays.
PATH = re.compile(r"""(?<![^\s"'=(])(?:(?:[A-Za-z]:|~|\.{1,2})?[\\/](?:[^\s\\/"'`]+[\\/])*|(?:[^\s\\/"'`]+[\\/]){2,})([^\s\\/"'`]*)""")


def shorten(text):
    return PATH.sub(lambda m: m.group(1), text.split("\n")[0])


def format_activity(block):
    """A tool call as the agent-models mod shows it."""
    args = block.get("input", {})
    key = next((k for k in ACTIVITY_KEYS if isinstance(args.get(k), str) and args[k]), None)
    return shorten(f"{block['name']}: {args[key]}") if key else block["name"]


def build_running_task(totals):
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


def build_turn_progress(totals):
    progress = [totals["activity"]] if totals["activity"] else []
    if totals["turn_start"]:
        begun = datetime.fromisoformat(totals["turn_start"].replace("Z", "+00:00"))
        elapsed = int((datetime.now(begun.tzinfo) - begun).total_seconds())
        progress.append(f"{elapsed // 60}m {elapsed % 60:02d}s")
    return progress


def format_tokens(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


# Per transcript: bytes read so far, message ids counted, token totals, the current turn's start and tool calls, and the last
# reply's model, effort, context size and tool call, and whether the transcript ends on a finished reply.
transcript_totals = {}


def scan_transcript(transcript):
    """Session token totals, current turn counters and last reply, reading only what was appended since the last call."""
    totals = transcript_totals.setdefault(
        transcript,
        {"offset": 0, "seen": set(), "in": 0, "out": 0, "turn_start": None, "turn_calls": 0, "turn_prompt": "", "turn_skill": None,
         "model": "?", "effort": "?", "context": 0, "activity": None, "ended": False},
    )
    # A subagent's transcript may not exist yet.
    try:
        with open(transcript, "rb") as f:
            f.seek(totals["offset"])
            data = f.read()
    except OSError:
        return totals
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
        if e.get("type") in ("user", "assistant"):
            totals["ended"] = (e.get("message") or {}).get("stop_reason") == "end_turn"
        message = e.get("message") if e.get("type") == "assistant" else None
        # API errors and limit notices are written as synthetic replies with no model, effort or usage of their own.
        if not message or e.get("isApiErrorMessage") or message.get("model") == "<synthetic>":
            continue
        totals.update(model=message.get("model", "?"), effort=e.get("effort", "?"))
        if e.get("attributionSkill"):
            totals["turn_skill"] = e["attributionSkill"]
        for block in message.get("content", []):
            if block.get("type") == "tool_use":
                totals["turn_calls"] += 1
                if block.get("name") not in SILENT_TOOLS:
                    totals["activity"] = format_activity(block)
        # A reply with several content blocks is written once per block, each with the same usage.
        if message.get("id") in totals["seen"]:
            continue
        totals["seen"].add(message.get("id"))
        u = message.get("usage", {})
        totals["context"] = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
        totals["in"] += totals["context"]
        totals["out"] += u.get("output_tokens", 0)
    return totals


def get_active_subagents(transcript):
    agents = []
    for meta_path in sorted(transcript.with_suffix("").glob("subagents/*.meta.json")):
        meta = read_json(meta_path)
        if meta is None or meta.get("finished"):
            continue
        agent_transcript = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
        totals = scan_transcript(agent_transcript)
        # Teammates and interrupted subagents never get "finished"; an idle one ends on a finished reply.
        if totals["ended"]:
            continue
        # A subagent's transcript may not exist yet; its meta is written at spawn.
        written = max(p.stat().st_mtime for p in (meta_path, agent_transcript) if p.exists())
        if time.time() - written > SUBAGENT_GONE_SECONDS:
            continue
        agent_type = meta.get("agentType", "")
        agents.append({
            "model": totals["model"],
            "effort": totals["effort"],
            "type": "" if agent_type in GENERIC_TYPES else agent_type,
            "activity": totals["activity"] or meta.get("description", ""),
        })
    return agents


def get_sessions():
    today = date.today()
    sessions = []
    skipped = False
    for path in SESSIONS_DIR.glob("*.json"):
        # A session file is Claude Code's: one that fails to read or holds unexpected values must not end the render.
        try:
            session = find_session(json.loads(path.read_text(encoding="utf-8")), today)
        except Exception as e:
            print(f"Session {path.name} skipped: {e!r}")
            skipped = True
            continue
        if session:
            sessions.append(session)
    shown = sorted(label_sessions(sessions), key=lambda s: s["cwd"].lower())
    # A file read mid-rewrite hides its session for one render; its footer and scans stay for the next.
    if not skipped:
        prune_session_stores({s["key"] for s in sessions}, {s["key"] for s in shown})
    return shown


def prune_session_stores(found, shown):
    """Drops what sessions no longer shown left in the per-session stores. A session hidden behind a namesake keeps its scans,
    so its transcripts are not read again from the start on every render."""
    for store in (statusline_inputs, statuslines, max_agent_rows):
        for key in store.keys() - shown:
            store.pop(key, None)
    # A session's transcript is <id>.jsonl; its subagents' are <id>/subagents/<agent>.jsonl.
    for transcript in [t for t in transcript_totals if not {t.stem, t.parent.parent.name} & found]:
        del transcript_totals[transcript]


def find_session(s, today):
    """The card data of a session shown today, or None for one that is not."""
    # updatedAt moves only on a status change, so a session busy or waiting since before midnight stays.
    status = s.get("status")
    if status not in ("busy", "waiting") and datetime.fromtimestamp(s.get("updatedAt", 0) / 1000).date() != today:
        return None
    # A spare is a background session started ahead of a job; it has no prompt or transcript yet.
    if s.get("spare"):
        return None
    cwd = s.get("cwd") or ""
    transcript = next(PROJECTS_DIR.glob(f"*/{s['sessionId']}.jsonl"), None)
    totals = scan_transcript(transcript) if transcript else {"model": "?", "effort": "?"}
    model, effort = totals["model"], totals["effort"]
    progress, footer = [], []
    if transcript:
        if status == "busy":
            progress = build_turn_progress(totals)
            footer.append(f"{totals['turn_calls']} tool calls")
        footer.append(f"{format_tokens(totals['in'])} in / {format_tokens(totals['out'])} out")
        statusline_inputs[s["sessionId"]] = {
            "session_id": s["sessionId"],
            "transcript_path": str(transcript),
            "cwd": cwd,
            "model": model,
            "context_tokens": totals["context"],
        }
    task = build_running_task(totals) if transcript else ""
    detail, highlight = task, bool(task)
    color = {"busy": "working", "waiting": "waiting"}.get(status, "idle")
    return {
        "key": s["sessionId"],
        "cwd": cwd,
        # A copied session is named "ARC (2)": the number is dropped so it matches its original.
        "name": re.sub(r"\s*\(\d+\)$", "", s.get("name") or Path(cwd).name),
        "updated": s.get("statusUpdatedAt") or s.get("updatedAt", 0),
        "model": model,
        "effort": str(effort),
        "detail": detail,
        "highlight": highlight,
        "color": color,
        "progress": " · ".join(progress),
        "footer": footer,
        "agents": get_active_subagents(transcript) if transcript else [],
    }


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


def build_model_effort(model, effort):
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


def get_git_bash():
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


def get_context_window(model):
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
    window = get_context_window(info["model"])
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


def build_ansi_html(text):
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


def run_statusline_command(bash, command, text, cwd):
    """The statusLine command's output for one session's JSON; empty when the run fails in any way, since command and output are the user's.
    A run past 10 s ends with every process it started: one left holding stdout would stall every footer and pill."""
    try:
        process = subprocess.Popen(
            [bash, "-c", command], cwd=cwd or None, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=True,
        )
    # ValueError: a NUL in the command.
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    job = add_to_new_job(process) if sys.platform == "win32" else None
    try:
        return process.communicate(text, timeout=10)[0] or ""
    except subprocess.TimeoutExpired:
        if job:
            job.kernel32.TerminateJobObject(job.handle, 1)
        else:
            os.killpg(process.pid, 9)
        process.kill()
        process.communicate()
        return ""
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        if job:
            job.kernel32.CloseHandle(job.handle)


def add_to_new_job(process):
    """A Windows job holding the process and every process it starts. MSYS fork and exec break the parent chain `taskkill /T` follows,
    so only a job reaches a git or sleep that bash started."""
    # ponytail: a child bash starts in the moment before the job takes it escapes; the run's own commands start well after.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    handle = ctypes.c_void_p(kernel32.CreateJobObjectW(None, None))
    kernel32.AssignProcessToJobObject(handle, ctypes.c_void_p(int(process._handle)))
    return SimpleNamespace(kernel32=kernel32, handle=handle)


def save_statuslines(lines, shared):
    # Written aside and renamed: a restart or window-close exit from another thread mid-write would leave an empty cache.
    temp = STATUSLINE_CACHE.with_name(STATUSLINE_CACHE.name + ".tmp")
    try:
        temp.write_text(json.dumps({"sessions": lines, "shared": shared}), encoding="utf-8")
        os.replace(temp, STATUSLINE_CACHE)
    except OSError:
        pass


def run_statuslines():
    """Runs the settings.json statusLine command for every session every 10 s and stores its parts."""
    global global_parts
    bash = get_git_bash()
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
        has_command = isinstance(command, str) and bool(command.strip())
        limits = read_json(CACHE / "rate-limits.json")
        # A shared part per key (5h, 7d, rtk, codemap); the session run last wins.
        lines, shared = {}, {}
        # ponytail: one run per session every 10 s (jq and git forks each); per render would start ~15 processes per second.
        for key, info in list(statusline_inputs.items()) if has_command else []:
            text = json.dumps(build_statusline_input(info, limits if isinstance(limits, dict) else None))
            parts = [p for p in run_statusline_command(bash, command, text, info["cwd"]).strip().split(" | ") if p.strip()]
            lines[key] = []
            for part in parts:
                match = GLOBAL_PART.search(ANSI.sub("", part))
                if match:
                    shared[match.group(1) or match.group(2)] = build_ansi_html(part)
                else:
                    lines[key].append(build_ansi_html(part))
        if not has_command:
            statuslines.clear()
        statuslines.update(lines)
        # No session to run for yet (the first pass beats the first render): the pills and the cache from before a restart stay.
        if lines or not has_command:
            global_parts = list(shared.values())
            save_statuslines(lines, global_parts)
        time.sleep(10)


def build_usage_pills():
    """The statusline parts every session shares, as pills under the cards."""
    return '<p class="usage">' + "".join(f'<span class="pill">{p}</span>' for p in global_parts) + '</p>' if global_parts else ""


# Most agent rows each card has shown since its session was last idle with no agents; cards pad to it so they never shrink mid-turn.
max_agent_rows = {}
# Scans, agent row counts and titles are read-modify-written; every request thread updates them.
render_lock = threading.Lock()


def update_sessions():
    """Scans every session and records the agent rows and title the page shows next; returns the sessions to show, each with
    the agent row count its card pads to."""
    with render_lock:
        sessions = get_sessions()
        for s in sessions:
            if s["color"] == "idle" and not s["agents"]:
                max_agent_rows.pop(s["key"], None)
            max_agent_rows[s["key"]] = max(max_agent_rows.get(s["key"], 0), len(s["agents"]))
            s["agent_rows"] = max_agent_rows[s["key"]]
        title = build_title(sessions)
        if not recent_titles or recent_titles[-1] != title:
            recent_titles.append(title)
        return sessions


def build_title_and_cards(sessions):
    """The tab title and the session cards' HTML."""
    cards = []
    for s in sessions:
        rows = [
            f'<div class="agent">● {build_model_effort(a["model"], a["effort"])} - '
            f'{html.escape(a["type"]) + " - " if a["type"] else ""}{html.escape(a["activity"])}</div>'
            for a in s["agents"]
        ]
        rows += ['<div class="agent">&nbsp;</div>'] * (s["agent_rows"] - len(rows))
        agents = "".join(rows)
        detail = html.escape(s["detail"])
        if s["highlight"]:
            detail = f'<span class="highlight">{detail}</span>'
        faded = " faded" if s["color"] == "idle" and time.time() - s["updated"] / 1000 > IDLE_FADE_SECONDS else ""
        cards.append(
            f'<div class="card {s["color"]}{faded}" data-key="{html.escape(s["key"])}">'
            f'<b class="name">{html.escape(s["label"])}</b> - {build_model_effort(s["model"], s["effort"])}'
            f'<div class="state">Running: {detail}</div>'
            f'<div class="progress">{STOPPED_LINES.get(s["color"]) or html.escape(s["progress"]) or "&nbsp;"}</div>{agents}'
            f'<div class="footer">{" | ".join(statuslines.get(s["key"], []) + [html.escape(f) for f in s["footer"]]) or "&nbsp;"}</div></div>'
        )
    return build_title(sessions), ("".join(cards) or '<p class="empty">No sessions today.</p>') + build_usage_pills()


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
        body { font-family: -apple-system, sans-serif; background: #0f172a; color: #f8fafc; padding: 1rem 2rem 2rem; max-width: 900px; margin: auto; }
        h1 { margin-top: 0.335em; }
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
    <h1>Claude Code Monitor</h1>
    <div id="sessions">__SESSIONS__</div>
    <p class="note" id="note"></p>
    <script>
        // A card never shrinks mid-turn: each keeps the tallest height it has had since its session was last idle with no agents.
        const maxHeights = {};
        function lockCardHeights() {
            for (const card of document.querySelectorAll(".card[data-key]")) {
                card.style.minHeight = "";
                const key = card.dataset.key;
                if (card.classList.contains("idle") && !card.querySelector(".agent")) delete maxHeights[key];
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
        if self.headers.get("Host") not in (f"{HOST}:{port}", f"localhost:{port}"):
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
        title, cards = build_title_and_cards(update_sessions())
        page = PAGE.replace("__VERSION__", VERSION).replace("__FIRST_PORT__", str(FIRST_PORT)).replace("__TITLE__", title).replace("__SESSIONS__", cards)
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
                current = [VERSION, *build_title_and_cards(update_sessions())]
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
        # Mid save-by-rename the script is briefly gone or locked; the next poll reads it again.
        try:
            current = str(SCRIPT.stat().st_mtime_ns)
            if current == seen:
                continue
            source = SCRIPT.read_text(encoding="utf-8")
            seen = current
            compile(source, str(SCRIPT), "exec")
        except (OSError, ValueError, SyntaxError) as e:
            print(f"New version not loaded: {e}")
            continue
        print("New version found, restarting.")
        start_with_instance_claim([sys.executable, str(SCRIPT), "--reloaded", str(port), str(instance_lock)])
        os._exit(0)


def start_with_instance_claim(command):
    """Starts a process that inherits this process's claim, so the claim outlives this process without a gap in which a
    waiting monitor takes it. The command passes the claim's handle on, so the process can hand it over again."""
    set_inheritable = os.set_handle_inheritable if sys.platform == "win32" else os.set_inheritable
    set_inheritable(instance_lock, True)
    return subprocess.Popen(command, close_fds=False)


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
    url = f"http://{HOST}:{port}/"
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
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    topmost = wintypes.HWND(-1)
    no_move_size_activate = 0x0001 | 0x0002 | 0x0010
    wm_close = 0x0010
    # An app window whose page failed to load has the bare host as its title; that page has no script to retry.
    error_title = f"{HOST}:{port}"
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
            # A browser that fails to start leaves no window, so the monitor exits as when the window closes.
            try:
                open_monitor_window(port)
            except OSError:
                pass
        time.sleep(3)


class MonitorServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port already in use, so a taken port would never be skipped. Posix needs it:
    # the reloaded server rebinds the port the old one left in TIME_WAIT.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


def start_free_server():
    """Binds the first free port from FIRST_PORT on."""
    for port in range(FIRST_PORT, FIRST_PORT + 100):
        try:
            return MonitorServer((HOST, port), MonitorHandler)
        except OSError:
            continue
    raise OSError(f"No free port in {FIRST_PORT}-{FIRST_PORT + 99}")


def start_reloaded_server(port):
    """Binds the port the open window uses, waiting for the older process to let it go."""
    for _ in range(40):
        try:
            return MonitorServer((HOST, port), MonitorHandler)
        except OSError:
            time.sleep(0.25)
    return MonitorServer((HOST, port), MonitorHandler)


def claim_single_instance():
    """True once no other monitor holds the claim, waiting up to 25 s; a start that finds it taken runs this. The claim lives as long as the
    process and any process it was handed to. A monitor whose window just closed holds the claim until it exits, so a
    session started in that time waits for it instead of leaving no monitor."""
    for _ in range(NO_VIEWER_EXIT_SECONDS + 5):
        if try_claim_instance():
            return True
        time.sleep(1)
    return False


INSTANCE_MUTEX = "Local\\cc_monitor"
# The claim's handle (Windows mutex) or file descriptor (posix lock).
instance_lock = None


def try_claim_instance():
    global instance_lock
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX)
        # NULL: the mutex exists but this process may not open it, as when an elevated session started the other monitor.
        if handle and ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            instance_lock = handle
            return True
        # An open handle keeps the mutex alive after the other monitor exits.
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    import fcntl
    lock = os.open(CACHE / "ccm.lock", os.O_WRONLY | os.O_CREAT)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock)
        return False
    instance_lock = lock
    return True


if __name__ == "__main__":
    # Start again detached and return, so the hooks and `! python ccm.py` in Claude Code return at once and
    # the monitor outlives them. On Windows it runs as pythonw.exe, without a console window.
    if os.environ.get("CCM_DETACHED") != "1":
        env = {**os.environ, "CCM_DETACHED": "1"}
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw) if sys.platform == "win32" and pythonw.exists() else sys.executable
        subprocess.Popen([exe, str(SCRIPT), *sys.argv[1:]], env=env, **DETACHED)
        sys.exit(0)
    CACHE.mkdir(parents=True, exist_ok=True)
    # A restart after an edit keeps the port, the claim and the window that is already open; the page reloads itself.
    reloaded = "--reloaded" in sys.argv
    if reloaded:
        reloaded_port, instance_lock = map(int, sys.argv[sys.argv.index("--reloaded") + 1:][:2])
    elif not try_claim_instance():
        # A live monitor shows this session; the wait covers one whose window just closed.
        if not claim_single_instance():
            sys.exit(0)
    server = start_reloaded_server(reloaded_port) if reloaded else start_free_server()
    port = server.server_address[1]
    print(f"Claude Monitor running at http://{HOST}:{port}")
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
