"""The macOS renderer must reach its helper without assuming a Linux procfs."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openkb.rendering import renderer


def test_macos_render_helper_protocol_does_not_require_procfs(tmp_path, monkeypatch):
    monkeypatch.setattr(renderer, "sys", SimpleNamespace(platform="darwin"), raising=False)
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            pytest.fail("macOS cannot read Linux process identity from /proc")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    instance = renderer.Renderer(tmp_path, assets_dir=tmp_path)
    try:
        value = instance._invoke(
            [sys.executable, "-c", "import json,sys; json.load(sys.stdin); print('{\"ok\":true}')"],
            {"kind": "math", "source": "a+b"},
        )
        assert value == {"ok": True}
    finally:
        instance.close()
