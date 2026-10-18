"""Artifact browsing and export use fixed master bytes, independently of copies."""

import io
import zipfile

import pytest
from test_storage_questions import question_knowledge

from ultrakb.application.artifacts import (
    artifact_files,
    export_artifact,
    list_artifacts,
    read_artifact,
    write_artifact_archive,
)
from ultrakb.application.views import storage_scope
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.execution_store import ExecutionStore


def artifact_writer(tmp_path):
    root, identity, publication = question_knowledge(tmp_path)
    execution = ExecutionStore(root, identity).begin(
        "artifact", fingerprint="artifact-export", max_calls=0
    )
    store = ArtifactStore(root, identity)

    def save(path, content):
        return store.save(
            path,
            content,
            expected_epoch=store.epoch(),
            execution_id=execution.execution_id,
            evidence_publication_id=publication.publication_id,
        )

    return root, identity, store, save


@pytest.mark.parametrize("parent_first", [True, False])
def test_artifact_file_and_directory_collision_retains_a_conflict_candidate(tmp_path, parent_first):
    _, _, store, save = artifact_writer(tmp_path)
    parent, child = "output/demo", "output/demo/child.md"
    first, second = (parent, child) if parent_first else (child, parent)
    assert save(first, b"Keep the accepted artifact.").status == "saved"
    candidate = save(second, b"Review the conflicting name.")
    assert candidate.status == "conflict"
    assert store.epoch() == 1
    assert store.read(first) == b"Keep the accepted artifact."
    with pytest.raises(FileNotFoundError):
        store.read(second)
    assert store.read(second, revision_id=candidate.revision_id) == b"Review the conflicting name."


def test_complete_artifact_export_uses_master_bytes_without_following_working_copies(tmp_path):
    root, identity, _, save = artifact_writer(tmp_path)
    expected = {
        "SKILL.md": b"# Accepted guide",
        "references/topic.md": b"Accepted supporting knowledge",
        "assets/example.bin": b"\x00\xff\x01\x02",
    }
    for name, payload in expected.items():
        assert save(f"output/skills/demo/{name}", payload).status == "saved"
    copy = root / "output/skills/demo"
    copy.mkdir(parents=True)
    (copy / "SKILL.md").write_text("Unaccepted external edit")
    (copy / "untracked.txt").write_text("Do not export uncommitted files")
    (copy / "assets").symlink_to(tmp_path, target_is_directory=True)
    scope = storage_scope(root, identity)
    assert artifact_files(root, "output/skills/demo", scope=scope) == (
        "output/skills/demo/SKILL.md",
        "output/skills/demo/assets/example.bin",
        "output/skills/demo/references/topic.md",
    )
    buffer = io.BytesIO()
    write_artifact_archive(root, "output/skills/demo", buffer, scope=scope)
    with zipfile.ZipFile(buffer) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == {
            f"demo/{name}": payload for name, payload in expected.items()
        }
    destination = tmp_path / "exports"
    destination.mkdir()
    first = export_artifact(root, "output/skills/demo", destination, scope=scope)
    second = export_artifact(root, "output/skills/demo", destination, scope=scope)
    assert first != second and first.read_bytes() == second.read_bytes()
    single = export_artifact(root, "output/skills/demo/SKILL.md", destination, scope=scope)
    assert single.read_bytes() == expected["SKILL.md"]
    assert (copy / "SKILL.md").read_text() == "Unaccepted external edit"


def test_paged_archive_keeps_one_epoch_while_writes_continue_and_gc_defers(tmp_path, monkeypatch):
    import ultrakb.storage.blobs as blob_io
    from ultrakb.storage.maintenance import Maintenance

    root, identity, store, save = artifact_writer(tmp_path)
    for number in range(70):
        save(f"output/skills/large/references/{number:03}.md", f"Original {number}".encode())
    epoch, copy_blob = store.epoch(), blob_io.copy_blob
    events = []

    def interleaved(db, digest, output):
        if not events:
            events.append(Maintenance(root, identity).collect(max_items=1).status)
            save("output/skills/large/references/069.md", b"A newer replacement")
            save("output/skills/large/references/late.md", b"A later added file")
        return copy_blob(db, digest, output)

    monkeypatch.setattr(blob_io, "copy_blob", interleaved)
    output = io.BytesIO()
    write_artifact_archive(root, "output/skills/large", output, scope=storage_scope(root, identity))
    with zipfile.ZipFile(output) as archive:
        assert len(archive.namelist()) == 70
        assert archive.read("large/references/069.md") == b"Original 69"
        assert "large/references/late.md" not in archive.namelist()
    assert events == ["busy"] and store.epoch() == epoch + 2
    assert Maintenance(root, identity).collect(max_items=1).status == "completed"
    first = store.files(prefix="output/skills/large/", epoch=epoch, limit=8)
    assert len(first.files) == 8 and first.epoch == epoch and first.next_after is not None
    cursor, names = first.next_after, [entry.path for entry in first.files]
    while cursor is not None:
        page = store.files(prefix="output/skills/large/", epoch=epoch, after=cursor, limit=8)
        names.extend(entry.path for entry in page.files)
        cursor = page.next_after
    assert len(names) == len(set(names)) == 70
    assert names[0].endswith("/000.md") and names[-1].endswith("/069.md")


def test_report_export_reads_its_exact_publication_and_rejects_other_roots(tmp_path):
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, _, _ = artifact_writer(tmp_path)
    knowledge = KnowledgeStore(root, identity)
    original = knowledge.bind()
    knowledge.publish(
        knowledge.prepare_changes(original, {"reports/quality/retained.md": b"# First report"})
    )
    scope = storage_scope(root, identity)
    knowledge.publish(
        knowledge.prepare_changes(
            knowledge.bind(), {"reports/quality/retained.md": b"# New report"}
        )
    )
    destination = tmp_path / "reports"
    destination.mkdir()
    target = export_artifact(root, "wiki/reports/quality/retained.md", destination, scope=scope)
    assert target.read_bytes() == b"# First report"
    output = io.BytesIO()
    write_artifact_archive(root, "wiki/reports/quality", output, scope=scope)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("quality/retained.md") == b"# First report"
    with pytest.raises(ValueError, match="different knowledge base"):
        export_artifact(
            tmp_path / "other-kb", "wiki/reports/quality/retained.md", destination, scope=scope
        )
    for path in (".ultrakb/kb.sqlite", "output/../.ultrakb/kb.sqlite", "wiki/sources/fixture.md"):
        with pytest.raises(ValueError):
            export_artifact(root, path, destination, scope=scope)
    with pytest.raises(ValueError, match="outside every knowledge base"):
        export_artifact(root, "wiki/reports/quality/retained.md", root, scope=scope)
    assert sorted(path.name for path in destination.iterdir()) == ["retained.md"]


def test_artifact_catalog_pages_fixed_versions_and_collapses_complete_packages(tmp_path):
    from ultrakb.storage.blobs import blob_path
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root, identity, store, save = artifact_writer(tmp_path)
    for path in (
        "output/skills/demo/SKILL.md",
        "output/skills/demo/references/a.md",
        "output/skills/demo0/SKILL.md",
        "output/decks/slides/index.html",
        "output/decks/slides-workspace/private.md",
        "output/.hidden.md",
        "output/visualize/graph.html",
        "output/standalone.md",
    ):
        save(path, path.encode())
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_changes(
            knowledge.bind(),
            {"reports/check.md": b"# Report", "explorations/note.md": b"# Note"},
        )
    )
    scope = storage_scope(root, identity)
    first = list_artifacts(root, scope=scope, limit=2)
    assert first.output_epoch == 8 and first.publication_id == scope.publication_id
    save("output/a-later-file.md", b"Not part of this catalog version")
    # Listing uses only manifest metadata even if one selected body is unavailable.
    blob_path(root, store.entry("output/standalone.md").digest).unlink()
    items, cursor = list(first.items), first.next_after
    while cursor is not None:
        page = list_artifacts(root, scope=scope, after=cursor, epoch=first.output_epoch, limit=2)
        assert len(page.items) <= 2 and page.output_epoch == first.output_epoch
        items.extend(page.items)
        assert page.next_after != cursor
        cursor = page.next_after
    assert [(item.path, item.kind) for item in items] == [
        ("output/decks/slides", "幻灯片"),
        ("output/skills/demo", "Skill"),
        ("output/skills/demo0", "Skill"),
        ("output/standalone.md", "文件"),
        ("output/visualize/graph.html", "知识图谱"),
        ("wiki/explorations/note.md", "探索笔记"),
        ("wiki/reports/check.md", "检查报告"),
    ]
    for arguments in ({"limit": 0}, {"after": "../escape"}, {"after": "wiki/sources/a.md"}):
        with pytest.raises(ValueError):
            list_artifacts(root, scope=scope, **arguments)


def test_artifact_export_cannot_write_inside_an_unmaterialized_main_store(tmp_path):
    from ultrakb.storage.database import create_database

    root, identity, _, save = artifact_writer(tmp_path)
    save("output/result.md", b"Accepted output")
    other = tmp_path / "other-main-store"
    create_database(other)
    destination = other / "exports"
    destination.mkdir()
    with pytest.raises(ValueError, match="outside every knowledge base"):
        export_artifact(root, "output/result.md", destination, scope=storage_scope(root, identity))
    assert not tuple(destination.iterdir())


def test_hidden_artifact_file_does_not_skip_visible_siblings_with_the_same_prefix(tmp_path):
    root, identity, _, save = artifact_writer(tmp_path)
    save("output/draft-workspace", b"Hidden temporary file")
    save("output/draft-workspace-summary.md", b"Visible accepted summary")
    save("output/z-visible.md", b"Another visible file")
    page = list_artifacts(root, scope=storage_scope(root, identity))
    assert [item.path for item in page.items] == [
        "output/draft-workspace-summary.md",
        "output/z-visible.md",
    ]


def test_selected_catalog_epoch_controls_public_read_and_export_after_a_later_write(tmp_path):
    root, identity, _, save = artifact_writer(tmp_path)
    path = "output/skills/selected/SKILL.md"
    save(path, b"The selected original")
    scope = storage_scope(root, identity)
    selected = list_artifacts(root, scope=scope)
    save(path, b"A later current version")
    save("output/skills/selected/references/late.md", b"A later new file")
    assert (
        read_artifact(root, path, scope=scope, epoch=selected.output_epoch)
        == "The selected original"
    )
    assert artifact_files(
        root, "output/skills/selected", scope=scope, epoch=selected.output_epoch
    ) == (path,)
    destination = tmp_path / "selected-export"
    destination.mkdir()
    exported = export_artifact(root, path, destination, scope=scope, epoch=selected.output_epoch)
    assert exported.read_bytes() == b"The selected original"
    output = io.BytesIO()
    write_artifact_archive(
        root, "output/skills/selected", output, scope=scope, epoch=selected.output_epoch
    )
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["selected/SKILL.md"]
        assert archive.read("selected/SKILL.md") == b"The selected original"


def test_binary_exports_stream_verified_originals_and_remove_failed_outputs(tmp_path, monkeypatch):
    from pathlib import Path

    from ultrakb.storage.blobs import blob_path

    root, identity, store, save = artifact_writer(tmp_path)
    path = "output/binary/large.bin"
    payload = bytes(range(256)) * (20 * 1024)
    save(path, payload)
    original = blob_path(root, store.entry(path).digest)
    read_bytes = Path.read_bytes

    def require_stream(file):
        if file == original:
            raise OSError("This original must be read as a stream")
        return read_bytes(file)

    monkeypatch.setattr(Path, "read_bytes", require_stream)
    scope, destination = storage_scope(root, identity), tmp_path / "binary-export"
    destination.mkdir()
    target = export_artifact(root, path, destination, scope=scope)
    assert target.read_bytes() == payload
    output = io.BytesIO()
    write_artifact_archive(root, "output/binary", output, scope=scope)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("binary/large.bin") == payload
    with original.open("r+b") as damaged:
        damaged.seek(1024 * 1024)
        damaged.write(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        export_artifact(root, path, destination, scope=scope)
    assert tuple(destination.iterdir()) == (target,)
