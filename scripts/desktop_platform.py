"""The native hosts, runtime downloads and installer names we build together."""

from __future__ import annotations

import platform
from dataclasses import dataclass


@dataclass(frozen=True)
class DesktopTarget:
    name: str
    system: str
    machine: str
    node_archive: str
    deb_arch: str | None = None


TARGETS = (
    DesktopTarget("debian-amd64", "Linux", "x86_64", "linux-x64.tar.xz", "amd64"),
    DesktopTarget("debian-arm64", "Linux", "arm64", "linux-arm64.tar.xz", "arm64"),
    DesktopTarget("windows-x64", "Windows", "x86_64", "win-x64.zip"),
    DesktopTarget("macos-arm64", "Darwin", "arm64", "darwin-arm64.tar.gz"),
)


def desktop_target(system: str | None = None, machine: str | None = None) -> DesktopTarget:
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    for target in TARGETS:
        if (target.system, target.machine) == (system, machine):
            return target
    raise ValueError(f"Unsupported desktop host: {system}/{machine}")
