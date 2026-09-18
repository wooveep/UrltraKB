"""OS measurements for an isolated worker and its descendant processes."""

import os
from functools import cache
from pathlib import Path


def memory_sample():
    if os.name == "nt":
        return _windows()
    if Path("/proc/meminfo").exists():
        values = {
            line.split(":", 1)[0]: int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if len(line.split()) >= 2 and line.split()[1].isdigit()
        }
        pending, seen, resident = [os.getpid()], set(), 0
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            try:
                status = Path(f"/proc/{pid}/status").read_text()
                resident += sum(
                    int(line.split()[1]) * 1024
                    for line in status.splitlines()
                    if line.startswith("VmRSS:")
                )
                for thread in Path(f"/proc/{pid}/task").iterdir():
                    pending.extend(int(p) for p in (thread / "children").read_text().split())
            except FileNotFoundError:
                continue  # An exited child no longer owns live memory.
        return {"available": values["MemAvailable"], "resident": resident, "private": None}
    # No guessed zero measurement on an unsupported platform.
    return None


@cache
def _windows_api():
    # ctypes keeps pointer types in a process-wide cache. Defining structures
    # per sample makes the observer itself retain memory on every admission.
    import ctypes
    from ctypes import wintypes

    class Status(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
            (name, ctypes.c_ulonglong)
            for name in ("total", "available", "page_total", "page_free", "virtual", "free", "ex")
        ]

    class Process(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD),
            ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD),
            ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD),
            ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
            ("name", wintypes.WCHAR * 260),
        ]

    class Memory(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak_ws",
                "ws",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
                "private",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Process),
    ]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    query = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
    return ctypes, Status, Process, Memory, kernel, query


def _windows():
    ctypes, Status, Process, Memory, kernel, query = _windows_api()
    status = Status()
    status.length = ctypes.sizeof(status)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError(ctypes.get_last_error())
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents = {}
    try:
        row = Process()
        row.size = ctypes.sizeof(row)
        found = kernel.Process32FirstW(handle, ctypes.byref(row))
        while found:
            parents[row.pid] = row.parent
            found = kernel.Process32NextW(handle, ctypes.byref(row))
    finally:
        kernel.CloseHandle(handle)
    children = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    pending, seen = [(os.getpid(), None)], set()
    resident = private = 0
    while pending:
        pid, parent_created = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        # Both process times and memory counters support this query right.
        # VM_READ is unnecessary and denied for some stale system descendants.
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # An exited leaf owns no memory. If it has live descendants, its
            # unverified ancestry cannot silently remove their memory as well.
            if ctypes.get_last_error() == 87 and not children.get(pid):
                continue
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            created = _creation_time(ctypes, kernel, handle)
            if parent_created is not None and created < parent_created:
                # Toolhelp retains parent PIDs after exit. A reused PID does not
                # make the original parent's surviving children our descendants.
                continue
            memory = Memory()
            memory.size = ctypes.sizeof(memory)
            if not query(handle, ctypes.byref(memory), memory.size):
                raise ctypes.WinError(ctypes.get_last_error())
            resident += memory.ws
            private += memory.private
            pending.extend((child, created) for child in children.get(pid, ()))
        finally:
            kernel.CloseHandle(handle)
    return {"available": status.available, "resident": resident, "private": private}


def _creation_time(ctypes, kernel, handle):
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
