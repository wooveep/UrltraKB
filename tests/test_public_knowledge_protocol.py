"""Public readers observe only complete, durably committed knowledge."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import pytest

from ultrakb.application.answers import save_exploration
from ultrakb.locks import kb_ingest_lock


def test_answer_save_publishes_immutable_knowledge_and_keeps_old_handle(kb_dir):
    from ultrakb.published_knowledge import initialize_publication, read_publication

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    previous = read_publication(kb_dir)
    saved = save_exploration(kb_dir, "Which answer?", "The complete answer.")
    current = read_publication(kb_dir)

    assert current.publication_id != previous.publication_id
    assert current.wiki_dir != kb_dir / "wiki"
    assert (
        (current.wiki_dir / "explorations/which-answer.md")
        .read_text()
        .endswith("The complete answer.\n")
    )
    assert not (previous.wiki_dir / "explorations/which-answer.md").exists()
    assert saved == kb_dir / "wiki/explorations/which-answer.md"


def test_committed_answer_recovers_public_switch_without_replaying_business(kb_dir, monkeypatch):
    from ultrakb.published_knowledge import (
        PublicationPending,
        PublicationUnavailable,
        current_pointer,
        initialize_publication,
        read_publication,
    )

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    previous = read_publication(kb_dir)
    replace = os.replace

    def fail_switch(source, target, *args, **kwargs):
        if target == current_pointer(kb_dir):
            raise OSError("Public pointer device failure")
        return replace(source, target, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_switch)
        with pytest.raises(PublicationPending) as interrupted:
            save_exploration(kb_dir, "Recover me", "Committed once.")
        assert read_publication(kb_dir).publication_id == previous.publication_id
        with pytest.raises(PublicationUnavailable):
            read_publication(kb_dir, interrupted.value.publication_id)
        assert (kb_dir / "wiki/explorations/recover-me.md").exists()
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        pass
    recovered = read_publication(kb_dir)
    assert recovered.publication_id != previous.publication_id
    assert (
        (recovered.wiki_dir / "explorations/recover-me.md")
        .read_text()
        .endswith("Committed once.\n")
    )
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))


def test_receipt_failure_keeps_new_public_knowledge_and_recovers_only_receipt(kb_dir, monkeypatch):
    from ultrakb.published_knowledge import initialize_publication, read_publication

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    previous = read_publication(kb_dir)
    replace = os.replace

    def fail_receipt(source, target, *args, **kwargs):
        if Path(target).name == "receipt.json":
            raise OSError("Receipt device failure")
        return replace(source, target, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_receipt)
        save_exploration(kb_dir, "Published answer", "Keep this visible.")
        current = read_publication(kb_dir)
        assert current.publication_id != previous.publication_id
        assert (current.wiki_dir / "explorations/published-answer.md").exists()
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        pass
    assert read_publication(kb_dir).publication_id == current.publication_id
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))


def test_nested_answer_saves_publish_once_after_outer_operation_commits(kb_dir):
    from ultrakb.mutation import mutation_scope
    from ultrakb.published_knowledge import initialize_publication, read_publication

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
        previous = read_publication(kb_dir)
        with mutation_scope(kb_dir, [kb_dir / "wiki"], operation="save-both-answers"):
            save_exploration(kb_dir, "First answer", "First.")
            assert read_publication(kb_dir).publication_id == previous.publication_id
            save_exploration(kb_dir, "Second answer", "Second.")
            assert read_publication(kb_dir).publication_id == previous.publication_id
    current = read_publication(kb_dir)
    assert (current.wiki_dir / "explorations/first-answer.md").exists()
    assert (current.wiki_dir / "explorations/second-answer.md").exists()
    assert len(list((kb_dir / ".ultrakb/published/records").iterdir())) == 2


def test_link_maintenance_publishes_repair_and_report_together(kb_dir, monkeypatch):
    from ultrakb.application.maintenance import LintOptions, check_knowledge
    from ultrakb.published_knowledge import (
        current_pointer,
        initialize_publication,
        read_publication,
    )

    (kb_dir / "wiki/concepts/topic.md").write_text("# Topic\n\nA [[missing]] link.")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    replace, switches = os.replace, []

    def observe_switch(source, target, *args, **kwargs):
        if target == current_pointer(kb_dir):
            switches.append(target)
        return replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", observe_switch)
    result = asyncio.run(check_knowledge(kb_dir, LintOptions(fix=True, semantic=False)))
    current = read_publication(kb_dir)
    assert result.status == "completed"
    assert "[[missing]]" not in (current.wiki_dir / "concepts/topic.md").read_text()
    assert list((current.wiki_dir / "reports").glob("lint_*.md"))
    assert len(switches) == 1


@pytest.mark.parametrize(
    "crash_at", ["sealed", "before_commit", "committed", "switched", "cleanup"]
)
def test_process_crash_exposes_complete_old_or_new_knowledge(kb_dir, crash_at):
    from ultrakb.published_knowledge import initialize_publication, read_publication

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    previous = read_publication(kb_dir)
    script = r"""
import json, os, sys
from pathlib import Path
from ultrakb.application.answers import save_exploration
root, phase = Path(sys.argv[1]), sys.argv[2]
replace, unlink = os.replace, Path.unlink
def interrupt_replace(source, target, *args, **kwargs):
    target = Path(target)
    data = json.loads(Path(source).read_text()) if target.suffix == '.json' else {}
    commit = target.parent.name == 'journal' and data.get('status') == 'committed'
    commit = commit and data.get('publication') is not None
    if phase == 'before_commit' and commit:
        os._exit(17)
    result = replace(source, target, *args, **kwargs)
    if phase == 'sealed' and target.name == 'record.json':
        os._exit(17)
    if phase == 'committed' and commit:
        os._exit(17)
    if phase == 'switched' and target == root / '.ultrakb/published/current.json':
        os._exit(17)
    return result
def interrupt_cleanup(path, *args, **kwargs):
    if phase == 'cleanup' and path.parent.name == 'journal' and path.suffix == '.json':
        os._exit(17)
    return unlink(path, *args, **kwargs)
os.replace, Path.unlink = interrupt_replace, interrupt_cleanup
save_exploration(root, 'Process result', 'Complete retained result.')
"""
    result = subprocess.run([sys.executable, "-c", script, str(kb_dir), crash_at], check=False)
    assert result.returncode == 17
    before_recovery = read_publication(kb_dir)
    switched = crash_at in {"switched", "cleanup"}
    assert (before_recovery.publication_id != previous.publication_id) == switched
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        pass
    current = read_publication(kb_dir)
    committed = crash_at in {"committed", "switched", "cleanup"}
    assert (current.publication_id != previous.publication_id) == committed
    assert (current.wiki_dir / "explorations/process-result.md").exists() == committed
    assert not (previous.wiki_dir / "explorations/process-result.md").exists()


def test_metadata_publication_reuses_knowledge_and_retains_original_resource_bytes(kb_dir):
    from ultrakb.locks import atomic_write_json, durable_unlink
    from ultrakb.mutation import mutation_scope
    from ultrakb.published_knowledge import initialize_publication, read_publication

    raw = kb_dir / "raw/retained.txt"
    raw.write_text("The original source.")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    previous = read_publication(kb_dir)
    hashes = kb_dir / ".ultrakb/hashes.json"
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        with mutation_scope(kb_dir, [hashes, raw], operation="withdraw-retained-resource"):
            atomic_write_json(hashes, {"retired": {"name": "Original source"}})
            durable_unlink(raw)
    current = read_publication(kb_dir)
    assert current.publication_id != previous.publication_id
    assert current.knowledge_revision_id == previous.knowledge_revision_id
    assert json.loads((previous.metadata_dir / ".ultrakb/hashes.json").read_text()) == {}
    assert json.loads((current.metadata_dir / ".ultrakb/hashes.json").read_text()) == {
        "retired": {"name": "Original source"}
    }
    assert (previous.metadata_dir / "raw/retained.txt").read_text() == "The original source."


@pytest.mark.parametrize("inside_child", [False, True])
def test_outer_process_crash_recovers_nested_preimages_before_next_publication(
    kb_dir, inside_child
):
    from ultrakb.published_knowledge import initialize_publication, read_publication

    original = kb_dir / "wiki/concepts/topic.md"
    original.write_text("Original page.")
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    previous = read_publication(kb_dir)
    script = r"""
import os, sys
from pathlib import Path
from ultrakb.application.answers import save_exploration
from ultrakb.locks import atomic_write_text, kb_ingest_lock
from ultrakb.mutation import mutation_scope
root, child = Path(sys.argv[1]), sys.argv[2] == 'True'
page = root / 'wiki/concepts/topic.md'
with kb_ingest_lock(root / '.ultrakb'):
    with mutation_scope(root, [page], operation='outer'):
        atomic_write_text(page, 'Outer intermediate page.')
        save_exploration(root, 'Nested result', 'Unpublished child commit.')
        if child:
            with mutation_scope(root, [page.parent], operation='inner-directory'):
                atomic_write_text(page, 'Inner intermediate page.')
                os._exit(17)
        os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(kb_dir), str(inside_child)], check=False
    )
    assert result.returncode == 17
    assert read_publication(kb_dir).publication_id == previous.publication_id
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        pass
    assert original.read_text() == "Original page."
    assert not (kb_dir / "wiki/explorations/nested-result.md").exists()
    assert read_publication(kb_dir).publication_id == previous.publication_id


def test_parallel_delegated_writes_keep_one_outer_rollback_and_public_identity(kb_dir):
    from ultrakb.locks import DelegatedWriteLease, atomic_write_text
    from ultrakb.mutation import mutation_scope
    from ultrakb.published_knowledge import initialize_publication, read_publication

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
        previous = read_publication(kb_dir)
        lease = DelegatedWriteLease(kb_dir / ".ultrakb/ingest.lock")

        def write(number):
            page = kb_dir / f"wiki/concepts/parallel-{number}.md"
            with mutation_scope(kb_dir, [page], operation="parallel-tool", delegated_lease=lease):
                atomic_write_text(page, f"Complete page {number}.")

        try:
            with pytest.raises(RuntimeError, match="Stop outer operation"):
                with mutation_scope(kb_dir, [], operation="parallel-tool-operation"):
                    contexts = [copy_context() for _ in range(12)]
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        futures = [
                            pool.submit(context.run, write, i) for i, context in enumerate(contexts)
                        ]
                        for future in futures:
                            future.result()
                    raise RuntimeError("Stop outer operation")
        finally:
            lease.revoke()
    assert not list((kb_dir / "wiki/concepts").glob("parallel-*.md"))
    assert read_publication(kb_dir).publication_id == previous.publication_id


def test_public_checkpoint_cleanup_recovers_without_execution_context(kb_dir, monkeypatch):
    from ultrakb.locks import atomic_write_text
    from ultrakb.mutation import mutation_scope
    from ultrakb.published_knowledge import initialize_publication, read_publication

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    checkpoint = kb_dir / ".ultrakb/compile-checkpoints" / ("a" * 32) / ("b" * 64)
    checkpoint.mkdir(parents=True)
    (checkpoint / "manifest.json").write_text("{}")
    remove = shutil.rmtree

    def fail_checkpoint(path, *args, **kwargs):
        if path == checkpoint:
            raise OSError("Checkpoint device failure")
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(shutil, "rmtree", fail_checkpoint)
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            page = kb_dir / "wiki/explorations/complete.md"
            with mutation_scope(kb_dir, [page], operation="completed-compile") as snapshot:
                atomic_write_text(page, "Complete public result.")
                snapshot.schedule_cleanup([checkpoint])
        current = read_publication(kb_dir)
        assert (
            current.wiki_dir / "explorations/complete.md"
        ).read_text() == "Complete public result."
        assert checkpoint.exists()
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        pass
    assert not checkpoint.exists()
    assert read_publication(kb_dir).publication_id == current.publication_id


def test_obsolete_pending_publication_cannot_modify_a_recreated_kb(kb_dir, monkeypatch):
    from ultrakb import config
    from ultrakb.application.knowledge_bases import initialize_kb
    from ultrakb.published_knowledge import (
        PublicationPending,
        current_pointer,
        initialize_publication,
        read_publication,
    )

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    replace = os.replace
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", kb_dir.parent / f"global-{kb_dir.name}.yaml")

    def fail_switch(source, target, *args, **kwargs):
        if target == current_pointer(kb_dir):
            raise OSError("Old public pointer failure")
        return replace(source, target, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_switch)
        with pytest.raises(PublicationPending) as interrupted:
            save_exploration(kb_dir, "Obsolete result", "Belongs to the old generation.")
    retained = interrupted.value.journal_path.read_bytes()
    shutil.rmtree(kb_dir)
    initialize_kb(kb_dir, seed_environment=False)
    replacement = read_publication(kb_dir)
    interrupted.value.journal_path.write_bytes(retained)
    with pytest.raises(PublicationPending):
        with kb_ingest_lock(kb_dir / ".ultrakb"):
            pytest.fail("An obsolete publication must never acquire the new writer")
    assert read_publication(kb_dir).publication_id == replacement.publication_id
    assert not (replacement.wiki_dir / "explorations/obsolete-result.md").exists()
    assert not (kb_dir / ".ultrakb/needs-repair.json").exists()


def test_explicit_writer_open_rebinds_a_copied_supported_snapshot(kb_dir, tmp_path, monkeypatch):
    from ultrakb import config
    from ultrakb.application.knowledge_bases import open_kb
    from ultrakb.published_knowledge import (
        PublicationUnavailable,
        initialize_publication,
        read_publication,
    )

    with kb_ingest_lock(kb_dir / ".ultrakb"):
        initialize_publication(kb_dir)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", tmp_path / "global.yaml")
    old = read_publication(kb_dir)
    copied = tmp_path / "copied"
    shutil.copytree(kb_dir, copied, ignore=shutil.ignore_patterns("copied"))
    with pytest.raises(PublicationUnavailable):
        read_publication(copied)
    assert open_kb(copied) == copied
    current = read_publication(copied)
    assert current.publication_id != old.publication_id
    assert current.generation != old.generation
    assert current.knowledge_revision_id == old.knowledge_revision_id
