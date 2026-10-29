import importlib.util
import time
import types
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


class Exited(Exception):
    pass


class StopTicks(Exception):
    pass


def load_ccm(clock, jump, max_ticks):
    """ccm with a fake clock: each sleep advances it by `jump`; the sleep after `max_ticks` raises StopTicks."""
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    ticks = {"count": 0, "max": max_ticks}

    def sleep(_):
        ticks["count"] += 1
        if ticks["count"] > ticks["max"]:
            raise StopTicks
        clock[0] += jump

    def exit_(code):
        raise Exited(ticks["count"])

    ccm.time = types.SimpleNamespace(time=lambda: clock[0], sleep=sleep)
    ccm.os = types.SimpleNamespace(_exit=exit_)
    return ccm, ticks


def test_monitor_survives_after_a_long_claim_wait():
    clock = [time.time()]
    start = clock[0]
    ccm, ticks = load_ccm(clock, 1, 1000)
    ccm.try_claim_instance = lambda: clock[0] - start >= 21  # old monitor exits 21 s after the session start
    assert ccm.claim_single_instance()
    ticks.update(count=0, max=3)
    try:
        ccm.exit_when_window_closed()
    except StopTicks:
        return
    except Exited as e:
        raise AssertionError(f"monitor exited {e.args[0]} s after claiming")


def test_clock_jump_does_not_exit_a_viewed_monitor():
    ccm, _ = load_ccm([time.time()], 3600, 3)  # each tick resumes after an hour asleep
    try:
        ccm.exit_when_window_closed()
    except StopTicks:
        return
    except Exited:
        raise AssertionError("monitor exited on the first check after the jump")


def test_monitor_exits_after_missed_heartbeats():
    ccm, _ = load_ccm([time.time()], 1, 1000)
    try:
        ccm.exit_when_window_closed()
    except Exited as e:
        assert e.args[0] == ccm.NO_VIEWER_EXIT_SECONDS + 1, e.args[0]
        return
    raise AssertionError("monitor never exited without a viewer")


def test_claim_gives_up_after_fixed_tries_despite_clock_jump():
    clock = [time.time()]
    ccm, _ = load_ccm(clock, -3600, 1000)  # clock steps back an hour on every tick
    tries = [0]

    def never():
        tries[0] += 1
        return False

    ccm.try_claim_instance = never
    assert not ccm.claim_single_instance()
    assert tries[0] == ccm.NO_VIEWER_EXIT_SECONDS + 5, tries[0]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
