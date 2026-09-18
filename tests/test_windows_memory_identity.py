"""Native process-tree accounting rejects descendants from a reused parent PID."""

import ctypes
from types import SimpleNamespace

import pytest

from openkb import resource_memory


class Status(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("available", ctypes.c_uint64)]


class Process(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("pid", ctypes.c_uint32), ("parent", ctypes.c_uint32)]


class Memory(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("ws", ctypes.c_uint64), ("private", ctypes.c_uint64)]


class NativeProcesses:
    def __init__(self, failure):
        self.failure = failure
        self.rows = iter([(744, 1), (8052, 744), (9000, 8052), (768, 744), (9999, 768)])
        self.created = {744: 100, 8052: 110, 9000: 120, 768: 1, 9999: 130}
        self.amounts = {744: 10, 8052: 20, 9000: 30, 768: 1000, 9999: 1000}
        self.error = 0
        self.opened, self.closed, self.measured = [], [], []

    def GlobalMemoryStatusEx(self, status):
        status._obj.available = 4096
        return True

    def CreateToolhelp32Snapshot(self, flags, pid):
        self.opened.append(10000)
        return 10000

    def Process32FirstW(self, handle, row):
        return self.Process32NextW(handle, row)

    def Process32NextW(self, handle, row):
        value = next(self.rows, None)
        if value is None:
            return False
        row._obj.pid, row._obj.parent = value
        return True

    def OpenProcess(self, rights, inherit, pid):
        if (pid == 768 and rights != 0x1000) or (pid == 9000 and self.failure == "denied"):
            self.error = 5
            return 0
        if (pid == 9000 and self.failure == "exited_leaf") or (
            pid == 8052 and self.failure == "exited_parent"
        ):
            self.error = 87
            return 0
        self.opened.append(pid)
        return pid

    def GetProcessTimes(self, handle, created, exited, kernel, user):
        created._obj.dwLowDateTime = self.created[handle]
        return True

    def query(self, handle, memory, size):
        self.measured.append(handle)
        memory._obj.ws = self.amounts[handle]
        memory._obj.private = self.amounts[handle] * 2
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)


@pytest.mark.parametrize("failure", [None, "denied", "exited_leaf", "exited_parent"])
def test_memory_sample_binds_descendants_to_the_current_parent_lifetime(monkeypatch, failure):
    native = NativeProcesses(failure)
    boundary = SimpleNamespace(
        sizeof=ctypes.sizeof,
        byref=ctypes.byref,
        c_void_p=ctypes.c_void_p,
        get_last_error=lambda: native.error,
        WinError=lambda code: OSError(code, "Native process access failed"),
    )
    monkeypatch.setattr(resource_memory, "os", SimpleNamespace(name="nt", getpid=lambda: 744))
    monkeypatch.setattr(
        resource_memory,
        "_windows_api",
        lambda: (boundary, Status, Process, Memory, native, native.query),
    )
    try:
        if failure in {"denied", "exited_parent"}:
            # A live protected child or unknown ancestry is never guessed as zero.
            with pytest.raises(OSError):
                resource_memory.memory_sample()
        else:
            measured = resource_memory.memory_sample()
            expected = 30 if failure == "exited_leaf" else 60
            assert measured == {"available": 4096, "resident": expected, "private": expected * 2}
        assert 768 not in native.measured and 9999 not in native.measured
    finally:
        assert sorted(native.closed) == sorted(native.opened)
