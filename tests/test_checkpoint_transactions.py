"""Complete computation checkpoints commit independently of knowledge publication."""

import json

import pytest

from ultrakb.locks import atomic_write_text, kb_ingest_lock
from ultrakb.mutation import mutation_scope, recover_pending_journals


@pytest.mark.parametrize("interrupted", [False, True])
def test_committed_index_survives_outer_failure_and_crash_recovery(
    kb_dir, monkeypatch, interrupted
):
    working = kb_dir / ".ultrakb/working/compile"
    marker = kb_dir / ".ultrakb/index-checkpoints/source/revision/checkpoint.json"
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        if interrupted:
            # Keep the active outer journal, as if the process died before rollback.
            monkeypatch.setattr(
                "ultrakb.mutation.MutationSnapshot.rollback_best_effort",
                lambda self: RuntimeError("Process interrupted"),
            )
        with pytest.raises(RuntimeError):
            with mutation_scope(kb_dir, [working], operation="compile-import-unit"):
                atomic_write_text(working / "draft.md", "Unpublished draft")
                with mutation_scope(
                    kb_dir, [marker.parent], operation="retain-complete-index", checkpoint=True
                ):
                    atomic_write_text(marker, '{"index":"complete"}')
                assert marker.is_file()
                raise RuntimeError("Compilation failed after indexing")
        if interrupted:
            monkeypatch.undo()
            recover_pending_journals(kb_dir, repairing=True)
            (kb_dir / ".ultrakb/needs-repair.json").unlink(missing_ok=True)
    assert json.loads(marker.read_text()) == {"index": "complete"}
    assert not (working / "draft.md").exists()
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))


def test_incomplete_checkpoint_rolls_back_without_joining_parent(kb_dir):
    directory = kb_dir / ".ultrakb/index-checkpoints/source/revision"
    marker = directory / "checkpoint.json"
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        with mutation_scope(kb_dir, [kb_dir / "working"], operation="compile-import-unit"):
            with pytest.raises(RuntimeError, match="Checkpoint write failed"):
                with mutation_scope(
                    kb_dir, [directory], operation="retain-complete-index", checkpoint=True
                ):
                    atomic_write_text(marker, "Incomplete")
                    raise RuntimeError("Checkpoint write failed")
            assert not marker.exists()


@pytest.mark.parametrize("outside", [True, False])
def test_independent_checkpoint_cannot_escape_or_overlap_publication(kb_dir, outside):
    parent = kb_dir / ".ultrakb/index-checkpoints/source"
    target = kb_dir / "wiki/concepts/unsafe.md" if outside else parent / "revision"
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        with mutation_scope(kb_dir, [parent], operation="parent"):
            before = set((kb_dir / ".ultrakb/journal").glob("*.json"))
            with pytest.raises(ValueError, match="checkpoint"):
                with mutation_scope(
                    kb_dir, [target], operation="retain-complete-index", checkpoint=True
                ):
                    pytest.fail("An independent checkpoint needs disjoint private paths")
            assert set((kb_dir / ".ultrakb/journal").glob("*.json")) == before
