"""Explicitly accept known artifact bytes into a real native store."""

from ultrakb.application.views import published_scope
from ultrakb.storage.artifact_store import ArtifactStore
from ultrakb.storage.execution_store import ExecutionStore


def publish_artifacts(kb_dir, directory, *, sync=False):
    bound = published_scope(kb_dir).knowledge
    executions = ExecutionStore(kb_dir, bound.identity)
    execution = executions.begin("fixture-artifact", fingerprint="explicit-fixture", max_calls=0)
    store = ArtifactStore(kb_dir, bound.identity)
    for path in directory.rglob("*"):
        if path.is_file():
            result = store.save(
                path.relative_to(kb_dir).as_posix(),
                path.read_bytes(),
                expected_epoch=store.epoch(),
                execution_id=execution.execution_id,
                evidence_publication_id=bound.publication_id,
            )
            assert result.status == "saved"
            if sync:
                assert (
                    store.sync(
                        path.relative_to(kb_dir).as_posix(), revision_id=result.revision_id
                    ).status
                    == "applied"
                )
    executions.finish(execution.execution_id, state="completed", work_settled=True)
