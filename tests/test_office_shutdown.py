"""An already-disposed UNO connection is an expected shutdown state."""

import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def shutdown(monkeypatch):
    from openkb.office import processes

    class DisposedException(Exception):
        pass

    modules = {
        "uno": {},
        "unohelper": {"Base": type("Base", (), {})},
        "com.sun.star.task": {"XInteractionHandler": type("XInteractionHandler", (), {})},
        "com.sun.star.lang": {"DisposedException": DisposedException},
    }
    for name, values in modules.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    worker = runpy.run_path(str(Path(processes.__file__).with_name("worker.py.txt")))
    return worker["close_office"], DisposedException


@pytest.mark.parametrize("disconnected", ["document", "desktop"])
def test_disconnected_shutdown_preserves_completed_conversion(shutdown, disconnected):
    close_office, disposed = shutdown
    calls = []

    def close(name):
        calls.append(name)
        if name == disconnected:
            raise disposed("Binary URP bridge disposed during call")

    document = SimpleNamespace(close=lambda _: close("document"))
    desktop = SimpleNamespace(terminate=lambda: close("desktop"))
    close_office(document, desktop)
    assert calls == ["document", "desktop"]


def test_shutdown_does_not_hide_unexpected_errors(shutdown):
    close_office, _ = shutdown

    def reject():
        raise ValueError("unexpected Office shutdown failure")

    with pytest.raises(ValueError, match="unexpected Office shutdown"):
        close_office(None, SimpleNamespace(terminate=reject))
