"""Page retirement reaches stable storage before its knowledge head commits."""

import asyncio
import uuid
from pathlib import Path

import pytest

from openkb.ingest_records import KnowledgeHead, KnowledgeRevision
from openkb.lifecycle import current_generation
from openkb.locks import kb_ingest_lock
from openkb.refresh_publication import RefreshProposal, commit_refresh
from openkb.unit_publication import wiki_versions

pytest_plugins = ("test_pdf_readback",)


def _trace_retirement(monkeypatch, old, publication):
    from openkb import locks, mutation

    events = []
    sync, unlink, write = locks._fsync_directory, Path.unlink, publication.write_record

    def track_sync(path):
        events.append(("fsync", path))
        return sync(path)

    def track_unlink(path, *args, **kwargs):
        if path == old:
            events.append(("unlink", path))
        return unlink(path, *args, **kwargs)

    def track_write(path, record):
        if path.name == "head.json":
            events.append(("head", path))
        return write(path, record)

    monkeypatch.setattr(locks, "_fsync_directory", track_sync)
    monkeypatch.setattr(mutation, "_fsync_directory", track_sync)
    monkeypatch.setattr(Path, "unlink", track_unlink)
    monkeypatch.setattr(publication, "write_record", track_write)
    return events


def _assert_flushed_before_head(events, old):
    deletion = events.index(("unlink", old))
    head = next(i for i, (kind, _) in enumerate(events) if kind == "head")
    assert ("fsync", old.parent) in events[deletion + 1 : head]


def test_refresh_flushes_retired_page_directory_before_head_commit(kb_dir, monkeypatch):
    from openkb import refresh_publication

    old = kb_dir / "wiki/summaries/retired.md"
    old.write_text("# Old knowledge")
    staging = kb_dir / ".openkb/staging/refresh"
    (staging / "wiki").mkdir(parents=True)
    proposal = RefreshProposal(
        proposal_id=uuid.uuid4().hex,
        view_id="legacy",
        expected_head=KnowledgeHead(view_id="legacy"),
        expected_kb_generation=current_generation(kb_dir),
        source_generations={},
        expected_pages=wiki_versions(kb_dir, kb_dir / "wiki"),
        candidate_pages={},
        candidate_manifest=KnowledgeRevision(
            knowledge_revision_id=uuid.uuid4().hex,
            view_id="legacy",
            base_revision_id=None,
            change_kind="refresh",
            input_revisions=(),
            page_dependencies={},
            generated_baselines={},
            original_references=(),
        ),
        inputs={},
        conflicts=(),
    )
    events = _trace_retirement(monkeypatch, old, refresh_publication)
    with kb_ingest_lock(kb_dir / ".openkb"):
        result = commit_refresh(kb_dir, staging, proposal)
    assert result.status == "completed" and not old.exists()
    _assert_flushed_before_head(events, old)


@pytest.mark.parametrize("fail_flush", [False, True])
def test_unit_publication_flushes_deletions_or_rolls_back(
    kb_dir, monkeypatch, pdf_model, fail_flush
):
    from openkb import locks, unit_publication
    from openkb.agent import compiler
    from openkb.application.documents import import_document
    from openkb.application.recompilation import recompile_document
    from openkb.application.views import view_scope
    from openkb.unit_publication import read_head

    source = kb_dir / "manual.txt"
    source.write_text("Fixture instructions.")
    first = import_document(kb_dir, source)
    assert first.status == "added", first.message
    scope = view_scope(kb_dir, first.units[0].view_id)
    old = scope.wiki_dir / "summaries/manual.md"
    head, content = read_head(kb_dir, scope.view_id), old.read_bytes()

    async def retire_summary(*args, scope, **kwargs):
        (scope.wiki_dir / "summaries/manual.md").unlink()

    monkeypatch.setattr(compiler, "compile_short_doc", retire_summary)
    events = _trace_retirement(monkeypatch, old, unit_publication)
    sync = locks._fsync_directory
    failed = False

    def fail_after_delete(path):
        nonlocal failed
        if fail_flush and not failed and path == old.parent and ("unlink", old) in events:
            failed = True
            raise OSError("Directory flush failed after retirement")
        return sync(path)

    monkeypatch.setattr(locks, "_fsync_directory", fail_after_delete)
    result = asyncio.run(recompile_document(kb_dir, first.source_id))
    if fail_flush:
        assert failed and result.status == "failed", result.message
        assert old.read_bytes() == content
        assert read_head(kb_dir, scope.view_id) == head
        assert not any(kind == "head" for kind, _ in events)
    else:
        assert result.status == "compiled", result.message
        assert not old.exists()
        _assert_flushed_before_head(events, old)
