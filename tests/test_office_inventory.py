"""Inventory failures identify the damaged runtime without weakening validation."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def office_tree(tmp_path, monkeypatch):
    from openkb.office import runtime
    from openkb.office.inventory import inventory
    from openkb.office.records import OfficeArtifact, OfficeManifest

    root = tmp_path / "office"
    contents = {
        "program/python.exe": b"private Python",
        "program/soffice.com": b"private Office",
        "openkb-provenance/office-launcher.exe": b"owned-process launcher",
        "share/fonts/font.ttf": b"private font",
        "LICENSE": b"runtime license",
    }
    for name, data in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    files, links = inventory(root)
    lock = json.loads(Path(runtime.__file__).with_name("runtime-lock.json").read_text("utf-8"))
    manifest = OfficeManifest(
        build_id=lock["build_id"],
        platform="win32",
        archive=OfficeArtifact(**lock["win32"]),
        source=OfficeArtifact(**lock["source"]),
        python_version=lock["python_version"],
        python="program/python.exe",
        soffice="program/soffice.com",
        launcher="openkb-provenance/office-launcher.exe",
        probe={},
        files=files,
        links=links,
        fonts={"share/fonts/font.ttf": files["share/fonts/font.ttf"]},
        licenses={"LICENSE": files["LICENSE"]},
        application_fonts_manifest="a" * 64,
    )
    (root / "openkb-office.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(runtime, "platform", SimpleNamespace(machine=lambda: "AMD64"))
    assert runtime.validate_runtime(root) == manifest
    return root


@pytest.mark.parametrize("difference", ["missing", "unexpected", "changed"])
@pytest.mark.parametrize("crash_dump", [False, True])
def test_inventory_failure_names_affected_files(office_tree, difference, crash_dump):
    from openkb.office.runtime import validate_runtime

    if crash_dump:
        (office_tree / "program/c896e54d-5473-4840-880b-33c0755dca17.dmp").write_bytes(b"MDMP")
    name = "program/python.exe" if difference != "unexpected" else "program/extra.pyc"
    path = office_tree / name
    if difference == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed runtime")
    with pytest.raises(
        ValueError, match="Office runtime file inventory changed or is incomplete"
    ) as caught:
        validate_runtime(office_tree)
    message = str(caught.value)
    assert difference in message
    assert name in message
    assert str(office_tree) in message
    assert "extract" in message.lower()


def test_inventory_stays_valid_after_moving_installation(office_tree):
    from openkb.office.runtime import validate_runtime

    moved = office_tree.with_name("安装目录 with spaces")
    office_tree.rename(moved)
    assert validate_runtime(moved).platform == "win32"


def test_inventory_difference_limits_output_but_keeps_total_count(office_tree):
    from openkb.office.runtime import validate_runtime

    for index in range(12):
        (office_tree / f"extra-{index:02}.tmp").write_bytes(b"unexpected file")
    with pytest.raises(ValueError) as caught:
        validate_runtime(office_tree)
    message = str(caught.value)
    assert "unexpected (12)" in message
    assert "extra-00.tmp" in message and "extra-04.tmp" in message
    assert "extra-05.tmp" not in message and "..." in message


def test_inventory_detects_retargeted_links(office_tree):
    from openkb.office.inventory import inventory
    from openkb.office.runtime import validate_runtime

    link = office_tree / "license-link"
    try:
        link.symlink_to("LICENSE")
    except OSError:
        pytest.skip("Host does not permit symbolic links")
    path = office_tree / "openkb-office.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["files"], manifest["links"] = inventory(office_tree)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    validate_runtime(office_tree)
    link.unlink()
    link.symlink_to(Path("program") / "python.exe")
    with pytest.raises(ValueError, match='links changed \\(1\\): \\["license-link"\\]'):
        validate_runtime(office_tree)


@pytest.mark.parametrize("contents", [b"MDMP" + b"\x00" * 28, b""])
def test_windows_crash_dump_does_not_block_next_conversion(office_tree, contents):
    from openkb.office.inventory import inventory
    from openkb.office.runtime import validate_runtime

    name = "program/c896e54d-5473-4840-880b-33c0755dca17.dmp"
    dump = office_tree / name
    dump.write_bytes(contents)
    manifest = (office_tree / "openkb-office.json").read_bytes()
    assert validate_runtime(office_tree).platform == "win32"
    # Keep crash evidence and the exact build inventory; only execution validation
    # recognizes the upstream diagnostic artifact. A dump can be incomplete.
    assert dump.read_bytes() == contents
    assert (office_tree / "openkb-office.json").read_bytes() == manifest
    assert name in inventory(office_tree)[0]


@pytest.mark.parametrize(
    "name",
    [
        "program/unexpected.dmp",
        "c896e54d-5473-4840-880b-33c0755dca17.dmp",
        "program/c896e54d-5473-4840-880b-33c0755dca17.dll",
    ],
)
def test_crash_dump_exception_does_not_accept_other_files(office_tree, name):
    from openkb.office.runtime import validate_runtime

    (office_tree / name).write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="unexpected"):
        validate_runtime(office_tree)


def test_crash_dump_exception_does_not_hide_changed_manifest_entry(office_tree):
    from openkb.office.inventory import inventory
    from openkb.office.runtime import validate_runtime

    dump = office_tree / "program/c896e54d-5473-4840-880b-33c0755dca17.dmp"
    dump.write_bytes(b"pinned file")
    path = office_tree / "openkb-office.json"
    manifest = json.loads(path.read_text("utf-8"))
    manifest["files"], manifest["links"] = inventory(office_tree)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    dump.write_bytes(b"changed pinned file")
    with pytest.raises(ValueError, match="files changed"):
        validate_runtime(office_tree)


def test_crash_dump_exception_does_not_accept_links(office_tree):
    from openkb.office.runtime import validate_runtime

    dump = office_tree / "program/c896e54d-5473-4840-880b-33c0755dca17.dmp"
    try:
        dump.symlink_to("python.exe")
    except OSError:
        pytest.skip("Host does not permit symbolic links")
    with pytest.raises(ValueError, match="unexpected"):
        validate_runtime(office_tree)
