"""Own isolated worker descendants until every execution process has exited."""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any


def isolated_target(target, arguments, ready) -> None:
    if os.name != "nt":
        os.setsid()
    # Windows must attach the process to its parent's job before code can
    # launch an OCR subprocess. The same barrier makes start failure safe.
    ready.wait()
    target(*arguments)


class ProcessTree:
    def __init__(self, process: Any, *, ready: Any = None):
        self.process = process
        self.ready = ready  # Keep the spawn semaphore alive through child unpickling.
        self.pid = process.pid
        self.job = _WindowsJob(self.pid) if os.name == "nt" else None

    def alive(self) -> bool:
        if self.job is not None:
            return self.job.active() != 0
        if self.process.is_alive():
            return True
        if Path("/proc").is_dir():
            found = False
            for name in os.listdir("/proc"):
                if not name.isdecimal():
                    continue
                try:
                    values = (Path("/proc") / name / "stat").read_text().rsplit(")", 1)[1].split()
                    if int(values[2]) == self.pid:
                        found = True
                        if values[0] != "Z":
                            return True
                except (OSError, ValueError, IndexError):
                    continue
            if found:
                return False  # Zombies have exited; the adopting OS owner reaps them.
        try:
            os.killpg(self.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def terminate(self, *, force: bool = False) -> None:
        if self.job is not None:
            self.job.terminate()
            return
        try:
            os.killpg(self.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            if self.process.is_alive():
                self.process.kill() if force else self.process.terminate()

    def close(self) -> None:
        if self.job is not None:
            self.job.close()


class _WindowsJob:
    def __init__(self, pid: int):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Counters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", Counters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.api = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        child = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            if not child:
                raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
            if not self.api.AssignProcessToJobObject(self.handle, child):
                raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        except BaseException:
            self.close()
            raise
        finally:
            if child:
                self.api.CloseHandle(child)

    def active(self) -> int:
        import ctypes
        from ctypes import wintypes

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        information = Accounting()
        if not self.api.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(information), ctypes.sizeof(information), None
        ):
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
        return information.ActiveProcesses

    def terminate(self) -> None:
        import ctypes

        if not self.api.TerminateJobObject(self.handle, 1):
            raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def resume_suspended_process(pid: int) -> None:
    """Resume a CREATE_SUSPENDED child after assigning its launcher to a job."""
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    api = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenThread.restype = wintypes.HANDLE
    api.ResumeThread.argtypes = [wintypes.HANDLE]
    api.ResumeThread.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = api.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        valid = api.Thread32First(snapshot, ctypes.byref(entry))
        while valid:
            if entry.th32OwnerProcessID == pid:
                thread = api.OpenThread(0x0002, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
                try:
                    if api.ResumeThread(thread) == 0xFFFFFFFF:
                        raise getattr(ctypes, "WinError")(getattr(ctypes, "get_last_error")())
                    return
                finally:
                    api.CloseHandle(thread)
            valid = api.Thread32Next(snapshot, ctypes.byref(entry))
        raise RuntimeError("Suspended interpreter has no primary thread")
    finally:
        api.CloseHandle(snapshot)
