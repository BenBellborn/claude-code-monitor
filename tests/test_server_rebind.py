import importlib.util
import sys
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"


def load_on(platform):
    """ccm imported as on that platform: the server class reads sys.platform in its body."""
    real = sys.platform
    sys.platform = platform
    try:
        spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
        ccm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ccm)
    finally:
        sys.platform = real
    return ccm


def test_posix_server_rebinds_a_port_in_time_wait():
    assert load_on("linux").MonitorServer.allow_reuse_address, "posix MonitorServer has SO_REUSEADDR off: the reloaded server cannot rebind"


def test_windows_server_skips_a_port_in_use():
    assert not load_on("win32").MonitorServer.allow_reuse_address, "Windows MonitorServer has SO_REUSEADDR on: a taken port is not skipped"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
