import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


def test_only_a_new_or_resumed_session_starts_the_monitor():
    hooks = json.loads((CCM_PATH.parent / "hooks" / "hooks.json").read_text())
    assert list(hooks["hooks"]) == ["SessionStart"], f"a hook other than SessionStart starts the monitor: {list(hooks['hooks'])}"
    assert hooks["hooks"]["SessionStart"][0]["matcher"] == "startup|resume", "/clear or compaction reopens a closed window"


def test_hook_command_runs_ccm_once_when_it_fails():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "runs.log"
    hooks = json.loads((CCM_PATH.parent / "hooks" / "hooks.json").read_text())
    # python3 either works (and ccm then fails) or is a stub that runs nothing, like the WindowsApps alias.
    for python3_works, expected in ((True, ["python3"]), (False, ["python"])):
        for name in ("python3", "python"):
            works = python3_works or name == "python"
            (tmp / name).write_text(
                f'#!/bin/sh\n{"" if works else "exit 126"}\n[ "$1" = -c ] && exit 0\necho {name} >> "$RUNS"\nexit 1\n', newline="\n")
            (tmp / name).chmod(0o755)
        for event, entries in hooks["hooks"].items():
            log.unlink(missing_ok=True)
            subprocess.run(
                [ccm.get_git_bash(), "-c", entries[0]["hooks"][0]["command"]], input="", check=True, timeout=30,
                env={**os.environ, "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}", "RUNS": str(log), "CLAUDE_PLUGIN_ROOT": str(tmp)},
            )
            runs = log.read_text().split() if log.exists() else []
            assert runs == expected, f"{event} hook ran ccm as {runs}, expected {expected}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
