"""One desktop per OS user, with local activation of the existing window.

The file lease arbitrates simultaneous launches before any workbench, tray or
task manager exists. Only its owner may replace a stale local-server endpoint.
CLI, API and spawned workers never enter this desktop-only boundary.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QStandardPaths
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def instance_directory() -> Path:
    location = QStandardPaths.StandardLocation.GenericConfigLocation
    return Path(QStandardPaths.writableLocation(location)) / "OpenKB"


class DesktopInstance(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        directory = instance_directory().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        identity = sha256(os.path.normcase(str(directory)).encode()).hexdigest()[:32]
        self.name = "UrltraKB-desktop-" + identity
        self.lock = QLockFile(str(directory / "desktop-instance.lock"))
        # A long-running GUI is never stale just because its lock is old.
        # QLockFile still detects an exited owner, including a crashed process.
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._activate)
        self.show_window: Callable[[], None] | None = None
        self.pending = False
        self.owned = False

    def start(self) -> bool:
        """Return True for the owner, False after an acknowledged activation."""
        deadline = time.monotonic() + 5
        while True:
            if self.lock.tryLock(0):
                self.owned = True
                QLocalServer.removeServer(self.name)
                if not self.server.listen(self.name):
                    message = self.server.errorString()
                    self.close()
                    raise RuntimeError(f"无法启动窗口通信：{message}")
                return True
            if self.lock.error() != QLockFile.LockError.LockFailedError:
                raise RuntimeError("无法访问当前用户的程序实例锁。请检查配置目录权限。")
            socket = QLocalSocket()
            try:
                socket.connectToServer(self.name)
                if socket.waitForConnected(200):
                    if socket.bytesAvailable() or socket.waitForReadyRead(200):
                        if bytes(socket.readAll()) == b"activated":
                            return False
            finally:
                socket.abort()
            if time.monotonic() >= deadline:
                raise RuntimeError("UrltraKB 已在运行，但暂时未响应。请稍后重试或从托盘打开。")
            time.sleep(0.05)

    def bind(self, show_window: Callable[[], None]) -> None:
        self.show_window = show_window
        if self.pending:
            self.pending = False
            show_window()

    def _activate(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if self.show_window is None:
                self.pending = True
            else:
                self.show_window()
            socket.disconnected.connect(socket.deleteLater)
            socket.write(b"activated")
            socket.flush()
            socket.disconnectFromServer()

    def close(self) -> None:
        if self.owned:
            self.server.close()
            QLocalServer.removeServer(self.name)
            self.lock.unlock()
            self.owned = False
