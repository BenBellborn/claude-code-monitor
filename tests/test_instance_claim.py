import ctypes
import importlib.util
import subprocess
import sys
import uuid
from ctypes import wintypes
from pathlib import Path

CCM_PATH = Path(__file__).resolve().parent.parent / "ccm.py"
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.restype = ctypes.c_void_p


def load_ccm():
    spec = importlib.util.spec_from_file_location("ccm", CCM_PATH)
    ccm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ccm)
    ccm.INSTANCE_MUTEX = f"Local\\ccm_test_{uuid.uuid4().hex}"  # never the real monitor's mutex
    return ccm


def test_restart_holds_the_claim_across_the_handoff():
    ccm = load_ccm()
    assert ccm.try_claim_instance()
    child = ccm.start_with_instance_claim([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        kernel32.CloseHandle(ctypes.c_void_p(ccm.instance_lock))  # the replaced monitor exits
        assert not ccm.try_claim_instance(), "a waiting monitor took the claim while the restarted one starts"
    finally:
        child.kill()
        child.wait()
    assert ccm.try_claim_instance(), "the claim outlived every process that held it"


class SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]


def test_mutex_this_process_cannot_open_counts_as_another_monitor():
    ccm = load_ccm()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    assert advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW("D:", 1, ctypes.byref(descriptor), None)  # empty DACL
    attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
    owner = kernel32.CreateMutexW(ctypes.byref(attributes), False, ccm.INSTANCE_MUTEX)
    assert owner, ctypes.get_last_error()
    try:
        assert ccm.try_claim_instance() is False, "a mutex that exists but cannot be opened (NULL handle) counts as no other monitor"
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(owner))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
