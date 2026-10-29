import importlib.util
import json
import tempfile
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
ccm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ccm)


def reply(message_id, output_tokens):
    return {"type": "assistant", "message": {"id": message_id, "model": "m", "content": [], "usage": {"input_tokens": 1, "output_tokens": output_tokens}}}


def test_shorten_keeps_words_and_cuts_paths_to_their_name():
    assert ccm.shorten("read and/or write") == "read and/or write"
    assert ccm.shorten("Read: src/app/main.py") == "Read: main.py"
    assert ccm.shorten("Read: C:\\Users\\Ben\\x.py") == "Read: x.py"
    assert ccm.shorten("first line\nsecond line") == "first line"


def test_read_prompt_text_turns_command_tags_into_the_typed_command():
    def prompt(text):
        return ccm.read_prompt_text({"message": {"content": text}})

    assert prompt("<command-name>/fix</command-name>\n<command-args>all</command-args>") == "/fix all"
    assert prompt("<command-name>/plan</command-name>") == "/plan"
    assert ccm.read_prompt_text({"message": {"content": [{"type": "text", "text": " hello "}]}}) == "hello"


def test_scan_transcript_counts_a_reply_once_and_waits_for_a_whole_line():
    path = Path(tempfile.mkdtemp()) / "t.jsonl"
    whole = json.dumps(reply("m1", 5)) + "\n"
    partial = json.dumps(reply("m2", 7))
    path.write_text(whole + whole + partial[:20], encoding="utf-8")
    assert ccm.scan_transcript(path)["out"] == 5, "a reply written once per block counted more than once"
    with path.open("a", encoding="utf-8") as f:
        f.write(partial[20:] + "\n")
    assert ccm.scan_transcript(path)["out"] == 12, "a line read half-written is lost"


def test_label_sessions_keeps_the_working_one_and_prefixes_clashing_folders():
    def session(cwd, color, updated):
        return {"cwd": cwd, "name": Path(cwd).name, "color": color, "updated": updated}

    working = session("C:/a/proj", "working", 1)
    kept = ccm.label_sessions([working, session("C:/a/proj", "idle", 2), session("C:/b/proj", "idle", 1)])
    assert working in kept and len(kept) == 2, kept
    assert sorted(s["label"] for s in kept) == ["a-proj", "b-proj"], kept
    assert [s["label"] for s in ccm.label_sessions([session("C:/a/solo", "idle", 1)])] == ["solo"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
