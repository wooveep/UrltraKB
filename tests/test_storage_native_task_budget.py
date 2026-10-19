"""Native work units keep one retained task and model budget across processes."""

import json
import multiprocessing
from uuid import uuid4

import pytest
from stream_transport import StreamTransport
from test_deck_validator import GOOD_DECK
from test_storage_deck_generation import deck_knowledge, tool_response

from ultrakb.application.knowledge_tasks import read_knowledge_execution
from ultrakb.config import save_config
from ultrakb.runtime.records import UnitResult
from ultrakb.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


def _native_unit(root, task_id, ordinal, parent_task_id):
    import ultrakb.application.views as views
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.generators import preview_generation
    from ultrakb.application.views import storage_scope
    from ultrakb.runtime.records import UnitIdentity
    from ultrakb.runtime.requests import GenerateArtifact
    from ultrakb.runtime.worker import _execute
    from ultrakb.storage.database import open_database

    with open_database(root, read_only=True) as db:
        scope = storage_scope(root, db.identity)
    name = f"value-{ordinal}"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(views, "view_scope", lambda *args: scope)
        version = preview_generation(root, "deck", name, scope=scope).version
        request = GenerateArtifact("deck", name, "Present value.", version, view_id="legacy")
        result = _execute(
            request,
            UnitIdentity.create(task_id, ordinal, str(root), request),
            ExecutionContext(parent_usage_task_id=parent_task_id),
        )
    (root.parent / f"native-{ordinal}.json").write_text(json.dumps(result.summary()))


def _run_unit(root, task_id, ordinal, *, parent_task_id=None):
    process = multiprocessing.get_context("spawn").Process(
        target=_native_unit, args=(root, task_id, ordinal, parent_task_id)
    )
    process.start()
    try:
        process.join(timeout=30)
        assert process.exitcode == 0, f"Native worker exit: {process.exitcode}"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    return UnitResult.from_summary(json.loads((root.parent / f"native-{ordinal}.json").read_text()))


@pytest.mark.parametrize("retry", [False, True])
def test_native_batch_units_share_the_persisted_budget_and_execution_links(tmp_path, retry):
    root, identity = deck_knowledge(tmp_path)
    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "language": "en",
            "model_policy": {"max_calls": 2, "retries": 0},
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
            "role_generation": {"answer": {"max_tokens": 128}},
        },
    )
    task_id = uuid4().hex
    second_task_id = uuid4().hex if retry else task_id
    responses = [
        item
        for ordinal in range(2)
        for item in (
            tool_response(
                "write_file",
                {"path": f"output/decks/value-{ordinal}/index.html", "content": GOOD_DECK},
                f"deck-{ordinal}",
            ),
            {"content": "Delivered."},
        )
    ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        first = _run_unit(root, task_id, 0)
        second = _run_unit(root, second_task_id, 1, parent_task_id=task_id if retry else None)
    assert first.status == "completed", first
    assert len(service.requests) == 2
    assert second.status == "blocked", second
    details = [
        read_knowledge_execution(
            root, identity=identity, execution_id=result.generation["execution_id"]
        )
        for result in (first, second)
    ]
    assert details[0].task.task_id == details[1].task.task_id
    for ordinal, detail in enumerate(details):
        assert detail.task.budget.used == 2 and detail.task.budget.remaining == 0
        assert detail.task.usage.requests == 2
        execution = next(
            item
            for item in detail.executions
            if item.execution_id == (first, second)[ordinal].generation["execution_id"]
        )
        assert execution.application_task_id == (task_id, second_task_id)[ordinal]
        assert execution.application_unit_id == str(ordinal)
        assert execution.parent_application_task_id == (task_id if retry and ordinal else None)
        from ultrakb.desktop.knowledge_tasks import detail_text

        assert f"工作单元：{ordinal}" in detail_text(detail)
    assert second.model_usage["current"]["requests"] == 0
    assert second.model_usage["history_status"] == "execution_only"
    assert (
        ArtifactStore(root, identity).read("output/decks/value-0/index.html") == GOOD_DECK.encode()
    )
    with pytest.raises(FileNotFoundError):
        ArtifactStore(root, identity).read("output/decks/value-1/index.html")


def test_application_binding_is_atomic_and_cannot_reset_policy_or_switch_budget(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from ultrakb.storage.coordination import storage_stage
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity = deck_knowledge(tmp_path)
    store, task_id, ready = ExecutionStore(root, identity), uuid4().hex, Barrier(2)

    def begin():
        ready.wait(timeout=5)
        try:
            return store.begin(
                "model_task",
                fingerprint="captured-policy",
                max_calls=2,
                application_task_id=task_id,
                application_unit_id="0",
            )
        except ValueError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: begin(), range(2)))
    accepted = [result for result in results if not isinstance(result, ValueError)]
    assert len(accepted) == 1
    first = accepted[0]
    store.finish(first.execution_id, state="completed", work_settled=True)
    assert store.application_execution(task_id) == first
    with pytest.raises(ValueError, match="reset policy"):
        store.begin(
            "model_task",
            fingerprint="changed-policy",
            max_calls=3,
            application_task_id=task_id,
            application_unit_id="1",
        )
    other = store.begin("model_task", fingerprint="captured-policy", max_calls=2)
    store.finish(other.execution_id, state="completed", work_settled=True)
    with pytest.raises(ValueError, match="replace its retained model budget"):
        store.begin(
            "model_task",
            fingerprint="captured-policy",
            max_calls=2,
            resume=other.execution_id,
            application_task_id=task_id,
            application_unit_id="0",
        )
    with storage_stage(root, identity, read_only=True) as db:
        assert db.connection.execute("SELECT count(*) FROM ukb_executions").fetchone()[0] == 2
        assert db.connection.execute(
            "SELECT DISTINCT model_budget_id FROM ukb_application_budget_bindings"
        ).fetchall() == [(first.model_budget_id,)]


def test_non_model_work_does_not_claim_the_native_model_budget(tmp_path):
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity = deck_knowledge(tmp_path)
    store, task_id = ExecutionStore(root, identity), uuid4().hex
    inspection = store.begin(
        "inspect",
        fingerprint="no-model",
        max_calls=0,
        application_task_id=task_id,
        application_unit_id="0",
    )
    store.finish(inspection.execution_id, state="completed", work_settled=True)
    assert store.application_execution(task_id) is None
    model = store.begin(
        "model_task",
        fingerprint="captured-policy",
        max_calls=2,
        application_task_id=task_id,
        application_unit_id="1",
    )
    assert model.model_budget_id != inspection.model_budget_id
    detail = read_knowledge_execution(root, inspection.execution_id, identity=identity)
    assert detail.execution.application_task_id == task_id
    assert detail.execution.application_unit_id == "0"


def test_one_native_retry_batch_preserves_each_sources_original_budget(tmp_path):
    from ultrakb.storage.execution_store import ExecutionStore

    root, identity = deck_knowledge(tmp_path)
    store, task_id = ExecutionStore(root, identity), uuid4().hex
    originals = []
    for _ in range(2):
        previous = store.begin("model_task", fingerprint="captured-policy", max_calls=2)
        store.finish(previous.execution_id, state="failed", work_settled=True)
        originals.append(previous)
    for ordinal, previous in enumerate(originals):
        resumed = store.begin(
            "model_task",
            fingerprint="captured-policy",
            max_calls=2,
            resume=previous.execution_id,
            application_task_id=task_id,
            application_unit_id=str(ordinal),
        )
        assert resumed.model_budget_id == previous.model_budget_id
        store.finish(resumed.execution_id, state="completed", work_settled=True)
        assert store.application_execution(task_id, unit_ordinal=str(ordinal)) == resumed
        if ordinal == 0:
            added = store.begin(
                "model_task",
                fingerprint="captured-policy",
                max_calls=2,
                application_task_id=task_id,
                application_unit_id="2",
            )
            assert added.model_budget_id not in {item.model_budget_id for item in originals}
            store.finish(added.execution_id, state="completed", work_settled=True)
    latest = store.begin(
        "model_task",
        fingerprint="captured-policy",
        max_calls=2,
        application_task_id=task_id,
        application_unit_id="3",
    )
    assert latest.model_budget_id == added.model_budget_id
    store.finish(latest.execution_id, state="completed", work_settled=True)
    original_unit = store.begin(
        "model_task",
        fingerprint="captured-policy",
        max_calls=2,
        application_task_id=task_id,
        application_unit_id="1",
    )
    assert original_unit.model_budget_id == originals[1].model_budget_id


def test_parent_only_continuation_preserves_the_original_deadline(tmp_path):
    from time import time

    from ultrakb.storage.execution_store import ExecutionStore

    root, identity = deck_knowledge(tmp_path)
    store, parent_task_id, deadline = ExecutionStore(root, identity), uuid4().hex, time() - 1
    first = store.begin(
        "model_task",
        fingerprint="captured-policy",
        max_calls=2,
        deadline_at=deadline,
        application_task_id=parent_task_id,
    )
    store.finish(first.execution_id, state="completed", work_settled=True)
    second = store.begin(
        "model_task",
        fingerprint="captured-policy",
        max_calls=2,
        deadline_at=time() + 300,
        application_task_id=uuid4().hex,
        parent_application_task_id=parent_task_id,
    )
    assert second.model_budget_id == first.model_budget_id
    assert store.budget(second.model_budget_id).deadline_at == deadline
