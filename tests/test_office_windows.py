"""Windows Office startup keeps the private runtime immutable and stays headless."""

import json
import os
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


def test_windows_office_launcher_does_not_create_console(tmp_path, monkeypatch):
    from openkb.office import processes

    no_window = 0x08000000
    monkeypatch.setattr(processes, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(processes.subprocess, "CREATE_NO_WINDOW", no_window, raising=False)

    def launch(command, **options):
        assert options.get("creationflags", 0) & no_window, (
            "Office runtime launcher would open a console window"
        )
        assert command[0] == str(tmp_path / "office-launcher.exe")
        return SimpleNamespace(returncode=0, poll=lambda: 0, wait=lambda **_: 0)

    monkeypatch.setattr(processes.subprocess, "Popen", launch)
    assert (
        processes.run_supervised(
            tmp_path,
            "python.exe",
            "office-launcher.exe",
            tmp_path,
            {"command": ["soffice.com", "--headless", "--version"], "timeout": 5},
            {},
            lambda: None,
        )
        == ""
    )


def test_windows_supervisor_does_not_create_worker_console(tmp_path, monkeypatch):
    from openkb.office import processes

    supervisor = runpy.run_path(str(Path(processes.__file__).with_name("supervisor.py.txt")))
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"command": ["python.exe", "-B", "-c", "pass"], "timeout": 5}),
        encoding="utf-8",
    )

    def launch(command, **options):
        assert options.get("creationflags", 0) & 0x08000000
        assert options.get("stdout") is sys.stdout
        assert options.get("stderr") is sys.stderr
        return SimpleNamespace(returncode=0, poll=lambda: 0, wait=lambda **_: 0)

    namespace = supervisor["main"].__globals__
    monkeypatch.setitem(
        namespace,
        "sys",
        SimpleNamespace(
            platform="win32", argv=["", str(request)], stdout=sys.stdout, stderr=sys.stderr
        ),
    )
    monkeypatch.setitem(
        namespace, "signal", SimpleNamespace(SIGTERM=15, SIGINT=2, signal=lambda *_: None)
    )
    monkeypatch.setitem(
        namespace, "subprocess", SimpleNamespace(Popen=launch, CREATE_NO_WINDOW=0x08000000)
    )
    assert supervisor["main"]() == 0


def test_windows_worker_does_not_create_office_console(tmp_path, monkeypatch):
    from openkb.office import processes

    modules = {
        "uno": {},
        "unohelper": {"Base": type("Base", (), {})},
        "com.sun.star.task": {"XInteractionHandler": type("XInteractionHandler", (), {})},
        "com.sun.star.lang": {"DisposedException": type("DisposedException", (Exception,), {})},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    worker = runpy.run_path(str(Path(processes.__file__).with_name("worker.py.txt")))
    source = tmp_path / "sample.docx"
    source.write_bytes(b"Office fixture")

    class SpawnChecked(Exception):
        pass

    def launch(command, **options):
        assert options.get("creationflags", 0) & 0x08000000
        assert options.get("stdout") is sys.stdout
        assert options.get("stderr") is sys.stderr
        assert command[0] == "soffice.com"
        raise SpawnChecked

    namespace = worker["convert"].__globals__
    monkeypatch.setitem(
        namespace,
        "sys",
        SimpleNamespace(platform="win32", stdout=sys.stdout, stderr=sys.stderr),
    )
    monkeypatch.setitem(
        namespace, "subprocess", SimpleNamespace(Popen=launch, CREATE_NO_WINDOW=0x08000000)
    )
    with pytest.raises(SpawnChecked):
        worker["convert"]({"input": str(source), "soffice": "soffice.com"}, tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires the native Windows console APIs")
def test_private_windows_python_and_descendants_have_no_visible_console(tmp_path):
    from openkb.office.policy import office_environment
    from openkb.office.processes import run_supervised
    from openkb.office.runtime import validate_runtime

    value = os.environ.get("OPENKB_TEST_OFFICE_RUNTIME")
    if not value:
        pytest.skip("Real Office integration needs a prepared OPENKB_TEST_OFFICE_RUNTIME")
    office_runtime = Path(value).resolve()
    manifest = validate_runtime(office_runtime)
    python = str(office_runtime / manifest.python)
    check = (
        "import ctypes; "
        "ctypes.windll.kernel32.GetConsoleWindow.restype=ctypes.c_void_p; "
        "ctypes.windll.user32.IsWindowVisible.argtypes=[ctypes.c_void_p]; "
        "window=ctypes.windll.kernel32.GetConsoleWindow(); "
        "assert not window or not ctypes.windll.user32.IsWindowVisible(window), "
        "'Visible Office console'; "
    )
    # The upstream wrapper launches its core interpreter without creation flags.
    # Verify that these uncontrolled descendants also stay invisible.
    code = check + (
        "import subprocess; "
        f"subprocess.run([{python!r},'-B','-c',{check!r}],check=True); "
        "print('headless Office descendants')"
    )
    output = run_supervised(
        office_runtime,
        manifest.python,
        manifest.launcher,
        tmp_path,
        {"command": [python, "-B", "-c", code], "timeout": 20},
        office_environment(tmp_path),
        lambda: None,
    )
    assert "headless Office descendants" in output
