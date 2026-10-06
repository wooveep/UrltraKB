"""Installed packages, source exports and frozen builds share one SDK baseline."""

from pathlib import Path

import pytest


def test_build_verifies_all_four_local_packages():
    from scripts.local_vendors import verify_local_vendors

    root = Path(__file__).resolve().parents[1]
    verified = verify_local_vendors(root)
    assert set(verified) == {"pageindex", "ictree", "pageindex-condb", "litellm"}
    for package in verified.values():
        assert package.is_relative_to(root / "vendor")
        assert (package / "_urltrakb_source.json").is_file()


@pytest.mark.parametrize(
    "file,component,license",
    [
        ("ChatIndex/ctree/ctree.py", "ictree@0.1.0+urltrakb.2", "Apache-2.0"),
        ("ConDB/contextdb/prompts/beam.jinja", "pageindex-condb@1.0+urltrakb.1", "Apache-2.0"),
        (
            "LiteLLM/litellm/model_prices_and_context_window_backup.json",
            "litellm@1.87.2+urltrakb.2",
            "MIT",
        ),
    ],
)
def test_inventory_attributes_vendor_sources_and_resources(monkeypatch, file, component, license):
    from importlib import metadata

    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    from scripts.inventory_desktop import Inputs

    inputs = Inputs(root, {"version": metadata.version("openkb"), "commit": "fixture"})
    key, path = inputs.owner(root / "vendor" / file)
    assert key == "python/" + component
    assert path == "vendor/" + file
    assert inputs.components[key]["declared_license"] == license
    assert len(inputs.components[key]["upstream_commit"]) == 40


@pytest.mark.parametrize("change", ["modify", "delete", "add", "nested_git"])
def test_source_audit_detects_unrecorded_changes(tmp_path, change):
    import shutil

    from scripts.local_vendors import verify_vendor_source

    source = Path(__file__).resolve().parents[1] / "vendor" / "ChatIndex"
    copied = tmp_path / "ChatIndex"
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns("__pycache__"))
    assert verify_vendor_source(copied) > 0
    if change == "modify":
        (copied / "ctree" / "ctree.py").write_text("# replaced\n")
    elif change == "delete":
        (copied / "LICENSE").unlink()
    elif change == "add":
        (copied / "ctree" / "unexpected.py").write_text("# untracked\n")
    else:
        (copied / "ctree" / ".git").mkdir()
    with pytest.raises(ValueError, match="source|Git"):
        verify_vendor_source(copied)


def test_installed_identity_and_checkout_origin_are_checked_separately(monkeypatch, tmp_path):
    import shutil
    from importlib import util
    from types import SimpleNamespace

    from scripts.local_vendors import verify_installed_vendors, verify_local_vendors

    root = Path(__file__).resolve().parents[1]
    package = tmp_path / "ctree"
    shutil.copytree(root / "vendor/ChatIndex/ctree", package)
    find_spec = util.find_spec
    monkeypatch.setattr(
        util,
        "find_spec",
        lambda name: (
            SimpleNamespace(origin=str(package / "__init__.py"))
            if name == "ctree"
            else find_spec(name)
        ),
    )
    assert verify_installed_vendors()["ictree"] == package
    with pytest.raises(ValueError, match="vendor/ChatIndex"):
        verify_local_vendors(root)
    (package / "_urltrakb_source.json").write_text('{"commit": "unverified"}')
    with pytest.raises(ValueError, match="source identity"):
        verify_installed_vendors()


def test_installed_identity_rejects_registry_version(monkeypatch):
    from importlib import metadata

    from scripts.local_vendors import verify_installed_vendors

    version = metadata.version
    monkeypatch.setattr(
        metadata, "version", lambda name: "1.87.2" if name == "litellm" else version(name)
    )
    with pytest.raises(ValueError, match="litellm metadata"):
        verify_installed_vendors()
