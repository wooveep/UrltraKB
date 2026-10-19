"""Discovery retains budget policy, recursive identities and independently paged diagnostics."""

from uuid import uuid4
from zipfile import ZipFile

import pytest
from stream_transport import StreamTransport
from test_storage_discovery import embedded_book, responses
from test_storage_questions import question_knowledge

from ultrakb.application.documents import import_document
from ultrakb.application.pending import process_pending, read_pending_job
from ultrakb.application.reprocessing import preview_reprocessing, reprocess_source
from ultrakb.application.views import storage_scope
from ultrakb.config import load_config, save_config
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.discovery_store import DiscoveryStore

pytestmark = pytest.mark.integration


def test_import_freezes_the_captured_extraction_budget(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    config_path = root / ".ultrakb/config.yaml"
    config = load_config(config_path)
    save_config(config_path, {**config, "extraction_budget": {"max_sources": 1}})
    with StreamTransport(responses("PARENT_BODY")) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        parent = import_document(root, source, scope=storage_scope(root, identity))
        assert parent.status == "added", parent.message
        save_config(config_path, {**config, "extraction_budget": {"max_sources": 5}})
        done = process_pending(root, scope=storage_scope(root, identity))
        [group] = done["groups"]
        assert group["budget"]["max_sources"] == 1
        assert group["budget_origin"] == "kb"
        assert done["jobs"][0]["status"] == "budget_wait" and done["imports_pending"] == 0
        assert len(service.requests) == 2


def test_long_object_keys_do_not_block_paging_all_private_object_diagnostics(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "private.xlsx"
    embedded_book(source, objects=0)
    long_key = "xl/embeddings/" + "segment/" * 140 + "Private.bin"
    with ZipFile(source, "a") as package:
        package.writestr(long_key, b"private")
        for index in range(65):
            package.writestr(f"xl/embeddings/Private{index:02}.bin", b"private")
    catalog = CatalogStore(root, identity)
    revision = catalog.admit(source, source_key="private")
    scope = storage_scope(root, identity)
    work = DiscoveryStore(root, identity).schedule(
        revision, root_import_id=uuid4().hex, view_id=scope.view_id
    )
    (root / ".ultrakb/config.yaml").unlink()
    done = process_pending(root, scope=scope)
    assert done["jobs"][0]["status"] == "completed", done
    first = read_pending_job(root, work.intent_id, scope=scope)
    following = read_pending_job(
        root, work.intent_id, scope=scope, checkpoint_after=first["next_checkpoint_after"]
    )
    objects = first["checkpoints"] + following["checkpoints"]
    assert len(first["checkpoints"]) == 63 and len(following["checkpoints"]) == 3
    assert len({item["checkpoint_id"] for item in objects}) == 66
    assert long_key in {item["object_key"] for item in objects}
    assert {item["outcome"] for item in objects} == {"private_object"}
    assert following["next_checkpoint_after"] is None
    assert not (root / ".ultrakb/catalog").exists()


def test_explicit_reprocessing_of_the_same_original_gets_independent_discovery_identity(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source)
    with StreamTransport(
        responses("PARENT_BODY", "CHILD_BODY", "PARENT_BODY", "CHILD_BODY")
    ) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        first = import_document(root, source, scope=storage_scope(root, identity))
        process_pending(root, scope=storage_scope(root, identity))
        scope = storage_scope(root, identity)
        preview = preview_reprocessing(root, first.source_id, scope=scope)
        assert preview["discovery"]["will_schedule"]
        source.unlink()
        second = reprocess_source(root, first.source_id, version=preview["version"], scope=scope)
        assert second.status == "added", second.message
        done = process_pending(root, scope=storage_scope(root, identity))
        discoveries = [job for job in done["jobs"] if job["kind"] == "discovery"]
        children = [job for job in done["jobs"] if job["kind"] == "import"]
        assert len(discoveries) == 2 and len(children) == 2
        assert len({job["source_id"] for job in children}) == 2
        assert len({job["root_import_id"] for job in discoveries}) == 2
        assert all(job["status"] == "completed" for job in (*discoveries, *children))
        assert len(service.requests) == 8


def test_recursive_workbooks_share_the_root_budget_but_keep_three_sources(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    child, source = tmp_path / "child.xlsx", tmp_path / "parent.xlsx"
    embedded_book(child)
    embedded_book(source, objects=0)
    with ZipFile(source, "a") as package:
        package.writestr("xl/embeddings/Child.xlsx", child.read_bytes())
    with StreamTransport(responses("PARENT_BODY", "PARENT_BODY", "CHILD_BODY")) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        import_document(root, source, scope=storage_scope(root, identity))
        source.unlink()
        child.unlink()
        done = process_pending(root, scope=storage_scope(root, identity))
        assert done["processed"] == 4 and done["runnable"] == 0, done
        assert len(done["groups"]) == 1 and done["groups"][0]["sources"] == 3
        assert done["groups"][0]["model_usage"]["actual_sends"] == 6
        assert len(CatalogStore(root, identity).source_page()) == 3
        assert len(service.requests) == 6


def test_long_embedded_filename_retains_its_label_without_using_it_as_a_physical_path(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    source = tmp_path / "parent.xlsx"
    embedded_book(source, objects=0)
    filename = "a" * 260 + ".txt"
    with ZipFile(source, "a") as package:
        package.writestr("xl/embeddings/" + filename, b"CHILD_BODY")
    revision = CatalogStore(root, identity).admit(source, source_key=str(source))
    scope = storage_scope(root, identity)
    DiscoveryStore(root, identity).schedule(
        revision, root_import_id=uuid4().hex, view_id=scope.view_id
    )
    with StreamTransport(responses("CHILD_BODY")) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        done = process_pending(root, scope=scope)
        assert [value["status"] for value in done["outcomes"]] == ["completed", "completed"], done
        [child] = [job for job in done["jobs"] if job["kind"] == "import"]
        catalog = CatalogStore(root, identity)
        assert catalog.source(child["source_id"]).label == filename
        assert catalog.read_original(child["source_revision_id"]) == b"CHILD_BODY"
        assert child["filename"] == filename
        assert len(service.requests) == 2
