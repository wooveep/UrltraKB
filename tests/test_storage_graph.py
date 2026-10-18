"""The real graph reader and writer share fixed main-store evidence."""

import pytest
from test_storage_questions import question_knowledge

from ultrakb.application.artifacts import generate_graph, read_artifact, read_graph
from ultrakb.application.views import storage_scope
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.usage_store import UsageStore


def graph_knowledge(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_changes(
            knowledge.bind(),
            {
                "concepts/a.md": (
                    b"---\ndescription: Fixed graph evidence\n---\n# A\n[[concepts/b]]"
                ),
                "concepts/b.md": b"# B",
            },
        )
    )
    return root, identity, storage_scope(root, identity)


def test_graph_reads_exact_publication_without_a_materialized_wiki(tmp_path):
    root, identity, scope = graph_knowledge(tmp_path)
    knowledge = KnowledgeStore(root, identity)
    knowledge.publish(
        knowledge.prepare_changes(knowledge.bind(), {"concepts/a.md": b"# A changed"})
    )
    unrelated = tmp_path / "unrelated"
    (unrelated / "concepts").mkdir(parents=True)
    (unrelated / "concepts/rogue.md").write_text("# Unaccepted working-copy page")
    (root / "wiki").symlink_to(unrelated, target_is_directory=True)
    graph = read_graph(root, scope=scope)
    assert graph["publication_id"] == scope.publication_id
    assert {node["id"] for node in graph["nodes"]} == {"concepts/a", "concepts/b"}
    assert graph["edges"] == [{"source": "concepts/a", "target": "concepts/b"}]
    assert graph["nodes"][0]["description"] == "Fixed graph evidence"
    assert read_graph(root, scope=storage_scope(root, identity))["edges"] == []


def test_graph_generation_accepts_original_html_with_a_zero_call_execution(tmp_path):
    root, identity, scope = graph_knowledge(tmp_path)
    result = generate_graph(root, scope=scope)
    assert result.graph == read_graph(root, scope=scope)
    assert result.path == root / "output/visualize/graph.html"
    assert result.artifact_revision.status == "saved"
    assert result.projection.status == "applied"
    assert ExecutionStore(root, identity).status(result.execution_id) == "completed"
    assert UsageStore(root, identity).requests(execution_id=result.execution_id).records == ()
    html = result.path.read_text()
    assert "<canvas" in html and "Fixed graph evidence" in html
    result.path.unlink()
    assert read_artifact(root, "output/visualize/graph.html", scope=scope) == html
    for name in ("journal", "model-tasks", "usage", "published-knowledge"):
        assert not (root / ".ultrakb" / name).exists()


@pytest.mark.parametrize("failure", ["copy", "stage_receipt"])
def test_accepted_graph_survives_copy_or_stage_receipt_failure(tmp_path, monkeypatch, failure):
    import ultrakb.storage.artifact_store as artifact_io
    from ultrakb.storage.coordination import storage_stage

    root, identity, scope = graph_knowledge(tmp_path)
    with monkeypatch.context() as failed:
        if failure == "copy":

            def unavailable(*args, **kwargs):
                raise OSError("The copy cannot be synchronized")

            failed.setattr(artifact_io, "atomic_write_bytes", unavailable)
        else:
            with storage_stage(root, identity) as db, db.transaction() as connection:
                connection.execute(
                    "CREATE TRIGGER fail_graph_stage BEFORE UPDATE OF state ON ukb_stage_attempts "
                    "WHEN OLD.stage_code = 'artifact.accept.v1' "
                    "BEGIN SELECT RAISE(ABORT, 'Graph stage receipt failed'); END"
                )
        result = generate_graph(root, scope=scope)
    assert result.artifact_revision.status == "saved" and result.path is None
    assert result.reason_code in {"artifact_sync_pending", "graph_confirmation_pending"}
    assert "<canvas" in read_artifact(root, "output/visualize/graph.html", scope=scope)
    if failure == "stage_receipt":
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute("DROP TRIGGER fail_graph_stage")
    store = artifact_io.ArtifactStore(root, identity)
    assert store.epoch() == 1
    assert store.sync(result.artifact_revision.path).status == "applied"
    assert (root / "output/visualize/graph.html").is_file()


@pytest.mark.parametrize("confirmation_failure", [False, True])
def test_graph_conflict_retains_html_without_replacing_a_concurrent_artifact(
    tmp_path, monkeypatch, confirmation_failure
):
    from ultrakb.storage.artifact_store import ArtifactStore
    from ultrakb.storage.coordination import storage_stage

    root, identity, scope = graph_knowledge(tmp_path)
    if confirmation_failure:
        with storage_stage(root, identity) as db, db.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER fail_conflict_stage BEFORE UPDATE OF state ON ukb_stage_attempts "
                "WHEN OLD.stage_code = 'artifact.accept.v1' "
                "BEGIN SELECT RAISE(ABORT, 'Conflict stage receipt failed'); END"
            )
    other = ExecutionStore(root, identity).begin("other_artifact", fingerprint="other", max_calls=0)
    save = ArtifactStore.save

    def concurrent(store, path, payload, **request):
        assert (
            save(
                store,
                "output/other.md",
                b"Concurrent output",
                expected_epoch=store.epoch(),
                execution_id=other.execution_id,
                evidence_publication_id=scope.publication_id,
            ).status
            == "saved"
        )
        return save(store, path, payload, **request)

    monkeypatch.setattr(ArtifactStore, "save", concurrent)
    result = generate_graph(root, scope=scope)
    assert result.artifact_revision.status == "conflict"
    assert result.reason_code == "artifact_conflict" and result.path is None
    assert result.confirmation_pending == confirmation_failure
    store = ArtifactStore(root, identity)
    assert b"<canvas" in store.read(
        result.artifact_revision.path, revision_id=result.artifact_revision.revision_id
    )
    assert store.read("output/other.md") == b"Concurrent output"
    assert store.epoch() == 1
    assert ExecutionStore(root, identity).status(result.execution_id) == "failed"


def test_durable_graph_stop_before_acceptance_leaves_no_output_head(tmp_path, monkeypatch):
    from ultrakb.locks import LockCancelled
    from ultrakb.storage.artifact_store import ArtifactStore

    root, identity, scope = graph_knowledge(tmp_path)
    save, stopped = ArtifactStore.save, []
    executions = ExecutionStore(root, identity)

    def stop(store, path, payload, **request):
        stopped.append(request["execution_id"])
        assert executions.request_stop(stopped[-1])
        return save(store, path, payload, **request)

    monkeypatch.setattr(ArtifactStore, "save", stop)
    with pytest.raises(LockCancelled):
        generate_graph(root, scope=scope)
    assert executions.status(stopped[0]) == "cancelled"
    assert ArtifactStore(root, identity).epoch() == 0


def test_stop_after_graph_acceptance_keeps_the_result_and_records_cancelled_execution(
    tmp_path, monkeypatch
):
    from ultrakb.storage.artifact_store import ArtifactStore

    root, identity, scope = graph_knowledge(tmp_path)
    save = ArtifactStore.save
    executions = ExecutionStore(root, identity)

    def stopped_after_acceptance(store, path, payload, **request):
        revision = save(store, path, payload, **request)
        assert revision.status == "saved"
        assert executions.request_stop(request["execution_id"])
        return revision

    monkeypatch.setattr(ArtifactStore, "save", stopped_after_acceptance)
    result = generate_graph(root, scope=scope)
    assert result.artifact_revision.status == "saved" and result.path is None
    assert executions.status(result.execution_id) == "cancelled"
    assert ArtifactStore(root, identity).epoch() == 1
    assert b"<canvas" in ArtifactStore(root, identity).read(result.artifact_revision.path)
    assert ArtifactStore(root, identity).sync(result.artifact_revision.path).status == "applied"


def test_graph_execution_receipt_failure_does_not_erase_the_applied_output(tmp_path):
    from ultrakb.storage.artifact_store import ArtifactStore
    from ultrakb.storage.coordination import storage_stage

    root, identity, scope = graph_knowledge(tmp_path)
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_graph_execution BEFORE UPDATE OF state ON ukb_executions "
            "BEGIN SELECT RAISE(ABORT, 'Execution receipt failed'); END"
        )
    result = generate_graph(root, scope=scope)
    assert result.artifact_revision.status == "saved" and result.projection.status == "applied"
    assert result.reason_code == "execution_confirmation_pending" and result.path.is_file()
    executions = ExecutionStore(root, identity)
    assert executions.status(result.execution_id) == "started"
    with storage_stage(root, identity) as db, db.transaction() as connection:
        connection.execute("DROP TRIGGER fail_graph_execution")
    assert executions.finish(result.execution_id, state="completed")
    assert ArtifactStore(root, identity).epoch() == 1


def test_durable_stop_at_graph_stage_admission_reports_cancellation(tmp_path, monkeypatch):
    from ultrakb.locks import LockCancelled
    from ultrakb.storage.artifact_store import ArtifactStore

    root, identity, scope = graph_knowledge(tmp_path)
    start, stopped = ExecutionStore.start_stage, []

    def stopping(store, execution_id, code, **work):
        if code == "graph.render.v1":
            stopped.append(execution_id)
            assert store.request_stop(execution_id)
        return start(store, execution_id, code, **work)

    monkeypatch.setattr(ExecutionStore, "start_stage", stopping)
    with pytest.raises(LockCancelled):
        generate_graph(root, scope=scope)
    assert ExecutionStore(root, identity).status(stopped[0]) == "cancelled"
    assert ArtifactStore(root, identity).epoch() == 0
