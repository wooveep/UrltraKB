"""Compile baselines copy complete Markdown and explicitly required attachments."""

import pytest


def test_private_compile_baseline_is_complete_fixed_and_releases_reclaim_lease(tmp_path):
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore
    from ultrakb.storage.maintenance import Maintenance

    root = tmp_path / "kb"
    identity = create_database(root)
    knowledge = KnowledgeStore(root, identity)
    files = {
        "index.md": b"[[entities/topic]]",
        "entities/topic.md": b"Aliases and shared concept summary",
        "AGENTS.md": b"Compilation policy",
        "images/needed.png": b"image needed by the current input",
        "images/unrelated.png": b"unrelated image" * 1000,
        "index/context.sqlite": b"an index database is not a compile input",
    }
    first = knowledge.publish(knowledge.prepare_revision(files, expected=None))
    bound = knowledge.bind(first.publication_id)
    with bound.compile_workspace(required_attachments=("images/needed.png",)) as baseline:
        directory = baseline.directory
        assert baseline.publication == first
        assert baseline.files == bound.files()
        assert {
            p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()
        } == {
            "index.md",
            "entities/topic.md",
            "AGENTS.md",
            "images/needed.png",
        }
        assert baseline.bytes_copied == sum(
            len(files[name])
            for name in (
                "index.md",
                "entities/topic.md",
                "AGENTS.md",
                "images/needed.png",
            )
        )
        # The private input survives model waits without holding a storage lease.
        assert Maintenance(root, identity).collect().status != "busy"
        knowledge.publish(
            knowledge.prepare_revision({"index.md": b"new"}, expected=first.publication_id)
        )
        assert (directory / "index.md").read_bytes() == files["index.md"]
        (directory / "index.md").write_bytes(b"candidate changes")
        assert bound.read("index.md") == files["index.md"]
    assert not directory.exists()


def test_private_compile_baseline_cannot_select_an_index_database(tmp_path):
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    knowledge = KnowledgeStore(root, create_database(root))
    knowledge.publish(knowledge.prepare_revision({"index/context.sqlite": b"index"}, expected=None))
    with pytest.raises(ValueError, match="Index databases"):
        with knowledge.bind().compile_workspace(required_attachments=("index/context.sqlite",)):
            pytest.fail("Index database copied into the compiler workspace")


def test_disposable_copy_omits_fsync_while_new_durable_content_still_syncs(tmp_path, monkeypatch):
    import os

    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    knowledge = KnowledgeStore(root, create_database(root))
    calls, original = [], os.fsync

    def observed(fd):
        calls.append(fd)
        return original(fd)

    monkeypatch.setattr(os, "fsync", observed)
    first = knowledge.publish(knowledge.prepare_revision({"index.md": b"durable"}, expected=None))
    assert calls
    calls.clear()
    with knowledge.bind().compile_workspace() as baseline:
        assert (baseline.directory / "index.md").read_bytes() == b"durable"
        assert calls == []
    knowledge.publish(
        knowledge.prepare_revision({"index.md": b"new durable"}, expected=first.publication_id)
    )
    assert calls


def test_publication_rejects_paths_that_cannot_be_materialized_portably(tmp_path):
    from ultrakb.storage.database import create_database
    from ultrakb.storage.knowledge_store import KnowledgeStore

    root = tmp_path / "kb"
    knowledge = KnowledgeStore(root, create_database(root))
    with pytest.raises(ValueError, match="logical relative"):
        knowledge.prepare_revision({"concepts/http:cache.md": b"content"}, expected=None)
