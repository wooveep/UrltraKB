"""Verify the installed Fcitx/Rime engine using actual X11 key events."""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--context", choices=("ibus", "fcitx"), default="fcitx")
    args = parser.parse_args()
    if args.context == "fcitx" and not os.environ.get("ULTRAKB_IME_QT_RUNTIME"):
        import PySide6

        library = str(Path(PySide6.__file__).parent / "Qt/lib")
        env = dict(
            os.environ,
            ULTRAKB_IME_QT_RUNTIME="1",
            LD_LIBRARY_PATH=library
            + (":" + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else ""),
        )
        os.execvpe(sys.executable, [sys.executable, __file__, *sys.argv[1:]], env)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    os.environ["QT_IM_MODULE"] = args.context
    if args.context == "fcitx":
        os.environ["QT_PLUGIN_PATH"] = "/usr/lib/x86_64-linux-gnu/qt6/plugins"
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtTest import QSignalSpy

    from ultrakb.desktop.conversation_view import QuestionEdit
    from ultrakb.desktop.verification import create_application

    app = create_application()
    assert app.platformName() == "xcb"
    events = []

    class Observe(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.InputMethod:
                events.append({"preedit": event.preeditString(), "commit": event.commitString()})
            return False

    observer = Observe()
    widget = QuestionEdit()
    widget.setWindowTitle("UltraKB 中文输入法验收")
    widget.resize(800, 144)
    widget.installEventFilter(observer)
    widget.show()
    widget.raise_()
    widget.activateWindow()
    widget.setFocus()
    spy = QSignalSpy(widget.submitted)
    xlib = ctypes.CDLL("libX11.so.6")
    xtest = ctypes.CDLL("libXtst.so.6")
    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    display = xlib.XOpenDisplay(None)
    assert display
    xlib.XStringToKeysym.argtypes = [ctypes.c_char_p]
    xlib.XStringToKeysym.restype = ctypes.c_ulong
    xlib.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    xlib.XKeysymToKeycode.restype = ctypes.c_uint
    xlib.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    xlib.XFlush.argtypes = [ctypes.c_void_p]
    xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xtest.XTestFakeKeyEvent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    xlib.XSetInputFocus(display, int(widget.winId()), 2, 0)
    xlib.XFlush(display)

    def settle(seconds=0.3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)

    def remote(*args):
        return subprocess.run(
            ["fcitx5-remote", *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    def key(name):
        code = xlib.XKeysymToKeycode(display, xlib.XStringToKeysym(name.encode()))
        assert code
        xtest.XTestFakeKeyEvent(display, code, 1, 0)
        xtest.XTestFakeKeyEvent(display, code, 0, 0)
        xlib.XFlush(display)
        settle(0.12)

    settle(0.7)
    deadline = time.monotonic() + 5
    while not remote("-n") and time.monotonic() < deadline:
        widget.activateWindow()
        widget.setFocus()
        xlib.XSetInputFocus(display, int(widget.winId()), 2, 0)
        xlib.XFlush(display)
        settle(0.1)
    previous, state = remote("-n"), remote()
    toggled = False
    try:
        remote("-s", "rime")
        remote("-o")
        settle()
        for letter in "nihao":
            key(letter)
        if not any(event["preedit"] for event in events):
            widget.clear()
            key("Shift_L")
            toggled = True
            for letter in "nihao":
                key(letter)
        assert any(event["preedit"] for event in events), events
        assert spy.count() == 0
        widget.grab().save(str(output / "preedit.png"))
        key("Return")
        assert spy.count() == 0, "IME confirmation must not send"
        widget.clear()
        for letter in "nihao":
            key(letter)
        key("space")
        assert "你好" in widget.toPlainText(), (widget.toPlainText(), events)
        assert spy.count() == 0
        widget.grab().save(str(output / "committed.png"))
        key("Return")
        assert spy.count() == 1
        report = {
            "platform": app.platformName(),
            "engine": remote("-n"),
            "context": args.context,
            "events": events,
            "commit": widget.toPlainText(),
            "submissions": spy.count(),
            "model_sends": 0,
            "evidence": "installed Fcitx/Rime Qt input context and XTest native key events; "
            "no injected QInputMethodEvent",
        }
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))
    finally:
        (output / "diagnostics.json").write_text(
            json.dumps(
                {
                    "events": events,
                    "text": widget.toPlainText(),
                    "focused": widget.hasFocus(),
                    "engine": remote("-n"),
                    "state": remote(),
                    "submissions": spy.count(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if toggled:
            key("Shift_L")
        if previous:
            remote("-s", previous)
        if state == "1":
            remote("-c")
        xlib.XCloseDisplay(display)
        widget.close()


if __name__ == "__main__":
    main()
