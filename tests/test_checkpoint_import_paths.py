"""Replay a validated index package through actual import/reprocessing failures."""

import json
import shutil

import pytest

pytest_plugins = ("test_import_capacity_recovery",)


@pytest.mark.parametrize("operation", ["import", "reprocess"])
def test_new_index_is_durable_before_compilation_and_reused_on_retry(
    retained_index, tmp_path, monkeypatch, operation
):
    from ultrakb.application.documents import import_document
    from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
    from ultrakb.application.source_retry import retry_source
    from ultrakb.compilation_report import CompilationIncomplete
    from ultrakb.condb_storage import ConDBPageIndexStorage
    from ultrakb.index_checkpoint import IndexCheckpoint
    from ultrakb.index_location import IndexLocation
    from ultrakb.index_packages import copy_index_package
    from ultrakb.indexer import IndexResult, _convert_pdf_to_pages, _write_long_doc_artifacts
    from ultrakb.state import HashRegistry
    from ultrakb.unit_publication import read_head

    root, admission, _, original_marker, service, _ = retained_index
    frozen = tmp_path / "frozen-index"
    shutil.copytree(original_marker.parent / "data/index", frozen)
    saved = IndexCheckpoint.model_validate_json(original_marker.read_text())
    built = []

    def replay_index(index_input, kb_dir, *, doc_name, scope, storage_path):
        # Historical index data, not an invented successful model reply. The real
        # reusable_index still validates, seals, hashes and commits this package.
        built.append(doc_name)
        copy_index_package(frozen, storage_path, document_id=saved.document_id)
        location = IndexLocation.package(storage_path)
        with ConDBPageIndexStorage(location) as storage:
            document = storage.get_document("default", saved.document_id)
        tree = {key: document[key] for key in ("doc_name", "doc_description", "structure")}
        pages = _convert_pdf_to_pages(index_input, doc_name, scope.wiki_dir / "sources/images")
        _write_long_doc_artifacts(tree, pages, doc_name, saved.document_id, kb_dir, scope=scope)
        return IndexResult(saved.document_id, saved.description, tree)

    async def fail_after_index(*args, **kwargs):
        raise CompilationIncomplete(("entity_generation_incomplete",), ("entities",))

    monkeypatch.setattr("ultrakb.indexer.index_long_document", replay_index)
    monkeypatch.setattr("ultrakb.agent.compiler.compile_long_doc", fail_after_index)
    head = read_head(root)
    if operation == "import":
        source = tmp_path / "fresh.pdf"
        shutil.copyfile(root / admission.revision.original, source)
        result = import_document(root, source)
    else:
        preview = preview_reprocessing(root, admission.source.source_id)
        assert preview["status"] == "ready"
        result = reprocess_source(root, admission.source.source_id, version=preview["version"])
    assert result.status == "failed", result.message
    [unit] = result.units
    assert unit.completed_stages == ("conversion", "indexing")
    marker = root / unit.recovery_point
    assert marker != original_marker and marker.is_file()
    retained = marker.read_bytes()
    manifest = json.loads(retained)
    assert {
        name: HashRegistry.hash_file(marker.parent / "data" / name) for name in manifest["files"]
    } == manifest["files"]
    assert read_head(root) == head and len(built) == 1

    if operation == "import":
        retried = retry_source(root, result.source_id)
    else:
        retried = reprocess_source(root, result.source_id, version=preview["version"])
    assert retried.status == "failed"
    assert len(built) == 1 and marker.read_bytes() == retained
    assert read_head(root) == head and service.requests == []
