"""The checkout and native builds must use the same editable PageIndex sources."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.local_pageindex import verify_local_pageindex

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_and_freezer_resolve_the_vendored_source():
    import pageindex

    source = verify_local_pageindex(ROOT)
    assert source == Path(pageindex.__file__).resolve().parent
    assert not (source.parent / ".git").exists()
    assert (source.parent / "LICENSE").is_file()


def test_freezer_rejects_another_environment_or_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "scripts.local_pageindex.util.find_spec",
        lambda name: SimpleNamespace(origin=str(tmp_path / "pageindex/__init__.py")),
    )
    with pytest.raises(ValueError, match="vendor/PageIndex"):
        verify_local_pageindex(ROOT)


def test_freezer_rejects_registry_metadata(monkeypatch):
    monkeypatch.setattr("scripts.local_pageindex.metadata.version", lambda name: "0.3.0.dev3")
    with pytest.raises(ValueError, match="version"):
        verify_local_pageindex(ROOT)


def test_native_inventory_attributes_editable_pageindex_to_its_source(monkeypatch):
    from importlib import metadata

    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    from scripts.inventory_desktop import Inputs

    inputs = Inputs(ROOT, {"version": metadata.version("openkb"), "commit": "fixture"})
    component, path = inputs.owner(ROOT / "vendor/PageIndex/pageindex/client.py")
    assert component == "python/pageindex@0.3.0.dev3+urltrakb.5"
    assert path == "vendor/PageIndex/pageindex/client.py"
    assert inputs.components[component]["upstream_commit"] == (
        "9ad54122bbd519cec8913198e2d63cff92781c1e"
    )
