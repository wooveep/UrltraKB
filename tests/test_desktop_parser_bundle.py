"""The freezer's selected physical files satisfy the real parser fingerprint boundary."""

import json
import shutil
import sys
import types
from pathlib import Path


def test_frozen_data_contains_every_parser_fingerprint_adapter(tmp_path, monkeypatch):
    from openkb.ocr import assembly

    repo = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    packaging = source / "packaging/desktop"
    packaging.mkdir(parents=True)
    assets = source / "openkb/rendering/assets"
    assets.mkdir(parents=True)
    (assets / "manifest.json").write_text("{}")
    fonts = source / "assets/fonts"
    fonts.mkdir(parents=True)
    (fonts / "manifest.json").write_text(json.dumps([]))
    shutil.copytree(repo / "openkb/ocr", source / "openkb/ocr")
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_data_files = lambda *args, **kwargs: []
    hooks.collect_submodules = lambda *args, **kwargs: []
    hooks.copy_metadata = lambda *args, **kwargs: []
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    selected = []

    def analyze(*args, **kwargs):
        selected.extend(kwargs["datas"])
        return types.SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

    spec = repo / "packaging/desktop/desktop.spec"
    exec(
        compile(spec.read_text(), str(spec), "exec"),
        {
            "SPECPATH": str(packaging),
            "Analysis": analyze,
            "PYZ": lambda *args: None,
            "EXE": lambda *args, **kwargs: None,
            "COLLECT": lambda *args, **kwargs: None,
        },
    )
    frozen = tmp_path / "frozen/openkb/ocr"
    frozen.mkdir(parents=True)
    for original, destination in selected:
        if destination == "openkb/ocr":
            shutil.copy2(original, frozen)
    monkeypatch.setattr(assembly, "__file__", str(frozen / "assembly.py"))
    for backend in ("system", "cloud", "local"):
        assert assembly.assembly_profile(backend)["adapters"]
