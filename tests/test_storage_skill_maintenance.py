"""Explicit rollback restores retained output without trusting edited projections."""

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.skill_maintenance import rollback_skill
from ultrakb.application.storage_execution import begin_storage_operation
from ultrakb.application.views import published_scope
from ultrakb.storage.artifact_store import ArtifactStore


def test_skill_rollback_uses_retained_iteration_and_preserves_current_output(tmp_path):
    root = tmp_path / "kb"
    initialize_kb(root)
    scope = published_scope(root)
    store = ArtifactStore(root, scope.knowledge.identity)
    paths = {
        "output/skills/example/SKILL.md": b"current content",
        "output/skills/example-workspace/iteration-1/SKILL.md": b"retained content",
    }
    epoch = store.epoch()
    context = ExecutionContext(storage_identity=scope.knowledge.identity)
    with begin_storage_operation(root, context, "accept-fixture-output"):
        for path, payload in paths.items():
            accepted = store.save(
                path,
                payload,
                expected_epoch=epoch,
                execution_id=context.storage_execution.execution_id,
                evidence_publication_id=scope.publication_id,
            )
            assert accepted.status == "saved"
            epoch = accepted.epoch
        context._storage_result = "completed"
    # The projection is absent: restoration must use the retained main-store files.
    assert not (root / "output/skills/example/SKILL.md").exists()
    restored = rollback_skill(root, "example", iteration=1)
    assert restored == root / "output/skills/example"
    assert store.read("output/skills/example/SKILL.md") == b"retained content"
    assert store.read("output/skills/example-workspace/iteration-2/SKILL.md") == b"current content"
    assert (restored / "SKILL.md").read_bytes() == b"retained content"
    assert published_scope(root).publication_id == scope.publication_id
