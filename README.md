# Claude Code Monitor (CCM)

A small window that shows every Claude Code session you used today, one card per session. It is one Python file, `ccm.py`, with no
packages to install, shipped as a Claude Code plugin.

![Claude Code Monitor with three sessions: one running, one blocked, one idle](docs/screenshot.png)

## Install
1. Install Python 3.10 or newer (see [Python](#python) below).
2. In Claude Code (v2.1.275 or later):
   ```text
   /plugin install ccm --marketplace BenBellborn/claude-code-monitor
   ```
   On older versions, run the two steps:
   ```text
   /plugin marketplace add BenBellborn/claude-code-monitor
   /plugin install ccm@claude-code-monitor
   ```
3. Start a new session. The monitor window opens.

**Updates:** turn on auto-update once: `/plugin` > **Marketplaces** > `claude-code-monitor` > **Enable auto-update**. Without it,
update by hand with `/plugin marketplace update claude-code-monitor`. The running monitor keeps the old version until you close its
window.

## What it shows
- **Title bar:** `🟢 2 ⛔ 1`, sessions running and sessions waiting on you (blocked or idle).
- **Each card:**
  - session name, model and effort;
  - `Running:` with the current or last skill or prompt, highlighted;
  - a second line: what the session is doing right now and for how long, or a red `Blocked` (awaiting user input) or red `Idle`;
  - one row per subagent that is working;
  - a footer with your own statusline (folder, git branch, ctx%), the turn's tool calls and the tokens in/out.
- **Under the cards:** pills with the parts every session shares, for example `5h:34%->17:00`, `7d:34%->Mon 17:00`.

## How it behaves
- Starts with every Claude Code session (a `SessionStart` hook). Only one runs at a time; a second start quits at once.
- Opens as its own app window (no tabs, no address bar) in Edge, Chrome or Chromium; without one of those, as a tab in your default
  browser.
- Closing the window stops it (after 20 s). The next session start opens it again.
- Listens on `127.0.0.1` only, on the first free port from 8080 to 8179.
- Reads `~/.claude/sessions`, `~/.claude/projects` (transcripts), `~/.claude/settings.json` (`statusLine`) and `~/.claude/cache`. Writes
  only to `~/.claude/cache`: the last statusline per session, so the cards are filled right after a restart, and a lock file on
  Mac and Linux.

## Needs
- Claude Code. On Windows also Git for Windows, which Claude Code already needs; the monitor runs your statusline through its bash.
- Python 3.10 or newer, as `python3` or `python` on the PATH. Standard library only.
- Optional: Edge, Chrome or Chromium for the app window.

| | Windows | Mac, Linux |
|---|---|---|
| Cards, statusline, title counts | yes | yes |
| One monitor at a time, closes with its window | yes | yes |
| App window | Edge or Chrome | Chrome, Edge or Chromium |
| Always on top, blank window replaced | yes | no |
| Tested | yes | not yet |

## Python
Check with `python3 --version` (Mac, Linux) or `python --version` (Windows).
- Windows: `winget install -e --id Python.Python.3.12 --scope user`. If `python` still opens the Microsoft Store, turn off the
  `python.exe` and `python3.exe` App execution aliases (Settings > Apps > Advanced app settings > App execution aliases).
- Mac: `brew install python`, or the installer from python.org. The Xcode tools' python3 is 3.9, too old.
- Linux: the distro package, for example `sudo apt install python3` or `sudo dnf install python3`.

## 5h and 7d pills
Claude Code passes the rate limits only to the statusline command, never to a file. The monitor runs your statusline itself, so to
show 5h and 7d your statusline script has to save them. In a bash statusline that reads its input into `$input` (needs `jq`), add:

```bash
# Rate limits reach only the statusline; saved for ccm.py.
limits=$(echo "$input" | jq -c '.rate_limits // empty')
[ -n "$limits" ] && echo "$limits" > "$HOME/.claude/cache/rate-limits.json.$$" && mv -f "$HOME/.claude/cache/rate-limits.json.$$" "$HOME/.claude/cache/rate-limits.json"
```

The statusline output is split on ` | `. A part that starts with `5h:` or `7d:`, or holds `rtk ` or `codemap `, goes to the pills;
every other part goes to the card footer. Model and effort are left out, since the card header shows them. No statusline at all
is fine: the footer then shows only tool calls and tokens.

## Stop or remove
- Stop: close the window; the process exits within 20 s. By hand: on Windows end the `pythonw.exe` running `ccm.py` in Task Manager;
  on Mac and Linux run `pkill -f ccm.py`.
- Remove: `/plugin uninstall ccm@claude-code-monitor`, then delete `~/.claude/cache/cc-monitor-statuslines.json` and
  `~/.claude/cache/ccm.lock`.

## Troubleshooting
- **Nothing opens:** a monitor is still running. Close its window, or stop it, and start a new session.
- **Window turns blank:** it opened while the monitor was restarting. On Windows the monitor replaces it within 6 s; F5 always works.
- **No footer:** check that your `statusLine.command` runs in bash, and that `jq` is installed if your script uses it.
- **Install or update fails with a git error:** without a GitHub SSH key, set the environment variable
  `CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1` so Claude Code clones over HTTPS, then try again.
- **Nothing at all:** run `python3 --version` or `python --version`; the plugin needs one of them on the PATH.

## License
MIT
