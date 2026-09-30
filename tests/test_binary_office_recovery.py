"""Old Office formats share durable import recovery, with format-specific diagnostics."""

import shutil
import struct
from pathlib import Path

import olefile
import pytest

pytest_plugins = ("pending_fixtures",)
FIXTURES = Path(__file__).parent / "fixtures/office"


def test_corrupt_xls_native_package_is_not_reported_as_missing_rebuilder(
    kb_dir, tmp_path, pdf_model
):
    from openkb.application.documents import import_document
    from openkb.application.pending import process_pending

    host = tmp_path / "bad-package.xls"
    shutil.copyfile(FIXTURES / "package-text.xls", host)
    with olefile.OleFileIO(host, write_mode=True) as compound:
        path = ["MBD0009CF7B", "\x01Ole10Native"]
        value = compound.openstream(path).read()
        compound.write_stream(path, b"\xff" * 4 + value[4:])
    import_document(kb_dir, host)
    result = process_pending(kb_dir)
    assert [item["outcome"] for item in result["checkpoints"]] == ["corrupt_object"]
    assert result["groups"][0]["sources"] == 1


@pytest.mark.parametrize(
    "fixture, extensions",
    [
        ("standard-object.xls", {"doc"}),
        ("historical-object.ppt", {"txt", "xls"}),
    ],
)
def test_orphan_and_historical_complete_files_survive_container_removal(
    kb_dir, tmp_path, pdf_model, office_runtime, prepared_cfb_helper, fixture, extensions
):
    from openkb.application.documents import import_document
    from openkb.application.pending import process_pending
    from openkb.application.removal import remove_document
    from openkb.application.sources import source_inventory
    from openkb.documents import read_document_source
    from openkb.source_catalog import list_sources

    original = tmp_path / fixture
    shutil.copyfile(FIXTURES / fixture, original)
    parent = import_document(kb_dir, original)
    remove_document(kb_dir, parent.source_id)
    original.unlink()
    result = process_pending(kb_dir)
    children = [s for s in list_sources(kb_dir) if s.source_id != parent.source_id]
    assert {s.name.rsplit(".", 1)[-1] for s in children} == extensions, result
    units = {item["source_id"]: item["units"] for item in source_inventory(kb_dir)}
    text = "\n".join(
        read_document_source(kb_dir, s.source_id, unit_id=unit["unit_id"])["content"]
        for s in children
        for unit in units[s.source_id]
    )
    assert ("EMBEDDED_WRITER_STANDARD_MARKER" if "doc" in extensions else "XLS_TAIL_VALUE") in text
    if "doc" in extensions:
        assert "private_object" in {c["outcome"] for c in result["checkpoints"]}


def test_compressed_ppt_waits_for_shared_budget_then_resumes_once(kb_dir, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.pending import process_pending, update_execution_budget
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.source_catalog import list_sources

    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"office_runtime_path": str(kb_dir / "missing-office")}
        ),
    )

    # Body conversion is unavailable, but original admission still commits discovery.
    parent = import_document(kb_dir, FIXTURES / "package-text.ppt")
    assert parent.status == "failed"
    from openkb.application.pending import pending_status

    group = pending_status(kb_dir)["groups"][0]["root_import_id"]
    update_execution_budget(kb_dir, group, {"max_decompressed_bytes": 39000})
    waiting = process_pending(kb_dir)
    assert waiting["jobs"][0]["status"] == "budget_wait"
    assert waiting["checkpoints"] == [] and waiting["groups"][0]["sources"] == 1
    assert process_pending(kb_dir)["processed"] == 0
    update_execution_budget(kb_dir, group, {"max_decompressed_bytes": 1000000})
    result = process_pending(kb_dir)
    assert result["imports_pending"] == 0 and len(list_sources(kb_dir)) == 2


def test_corrupt_ppt_compression_keeps_a_diagnostic_without_an_import(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.application.pending import process_pending

    host = tmp_path / "corrupt.ppt"
    shutil.copyfile(FIXTURES / "package-text.ppt", host)
    with olefile.OleFileIO(host, write_mode=True) as compound:
        data = bytearray(compound.openstream("PowerPoint Document").read())
        # Fixture's independently inspected ExOleObjStg record begins at 37067.
        assert struct.unpack_from("<HHI", data, 37067) == (16, 0x1011, 497)
        data[37067 + 8 + 496] ^= 1
        compound.write_stream("PowerPoint Document", bytes(data))
    import_document(kb_dir, host)
    result = process_pending(kb_dir)
    assert [c["outcome"] for c in result["checkpoints"]] == ["corrupt_object"]
    assert result["groups"][0]["sources"] == 1


@pytest.mark.parametrize("host", ["xls", "ppt"])
def test_non_file_objects_have_specific_diagnostics(kb_dir, pdf_model, host):
    from openkb.application.documents import import_document
    from openkb.application.pending import process_pending

    import_document(kb_dir, FIXTURES / ("object-diagnostics." + host))
    result = process_pending(kb_dir)
    outcomes = {c["outcome"] for c in result["checkpoints"]}
    assert {"external_reference", "preview", "private_object"} <= outcomes
    assert result["groups"][0]["sources"] == (2 if host == "ppt" else 1)


@pytest.mark.parametrize("corrupt_policy", [False, True])
def test_legacy_discovery_keeps_recursive_host_coverage(
    kb_dir, pdf_model, office_runtime, prepared_cfb_helper, corrupt_policy
):
    import json

    from openkb.application.documents import import_document
    from openkb.application.pending import pending_status, process_pending
    from openkb.documents import read_document_source
    from openkb.source_catalog import list_sources

    parent = import_document(kb_dir, FIXTURES / "legacy-nested-xls.doc")
    job = pending_status(kb_dir)["jobs"][0]
    path = kb_dir / ".openkb/catalog/discovery-intents" / (job["id"] + ".json")
    saved = json.loads(path.read_text())
    saved["policy"] = "office-embedded-files-v2"
    path.write_text(json.dumps(saved))  # Persisted state from the previous release.
    process_pending(kb_dir, max_jobs=1)
    if corrupt_policy:
        child = next(
            j for j in pending_status(kb_dir)["jobs"] if j.get("filename", "").endswith(".xls")
        )
        checkpoint = kb_dir / ".openkb/catalog/discovery-checkpoints" / (child["id"] + ".json")
        altered = json.loads(checkpoint.read_text())
        altered["policy"] = "office-embedded-files-v3"
        checkpoint.write_text(json.dumps(altered))
    result = process_pending(kb_dir)
    children = [s for s in list_sources(kb_dir) if s.source_id != parent.source_id]
    docs = [s for s in children if s.name.endswith(".doc")]
    assert len(docs) == 1 and result["groups"][0]["sources"] == 3, result
    if corrupt_policy:
        assert next(j for j in result["jobs"] if j["id"] == child["id"])["status"] == "failed"
    assert (
        "EMBEDDED_WRITER_STANDARD_MARKER"
        in read_document_source(kb_dir, docs[0].source_id)["content"]
    )
