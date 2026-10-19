"""Native conversation commits retain authored history and pending submissions."""

import pytest
from error_transport import ErrorTransport
from native_usage_fixtures import read_requests
from test_storage_questions import question_knowledge

from ultrakb.application.conversations import continue_conversation, read_conversation
from ultrakb.application.views import storage_scope
from ultrakb.config import load_config, save_config
from ultrakb.storage.session_store import SessionStore

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("assistant_text", ["", " \n\t"])
def test_unfinished_commit_keeps_pending_submission_and_authored_history(tmp_path, assistant_text):
    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("authored", model="openai/gpt-4o-mini", language="en", title="用户手工保留的标题")
    store.append_exchange("authored", "original", "作者保存的原文问题", "作者保存的核对记录")
    question = "  保留换行\n以及原始问题  "
    store.queue_submission(
        "authored", "waiting-answer", question, view_id="legacy", order=7, after_turn=1
    )
    before = read_conversation(root, "authored", scope=storage_scope(root, identity))
    with pytest.raises(ValueError, match="committed.*original"):
        store.append_exchange(
            "authored", "unfinished", question, assistant_text, attempt_id="waiting-answer"
        )
    after = read_conversation(root, "authored", scope=storage_scope(root, identity))
    assert after == before
    assert after.turns == (("作者保存的原文问题", "作者保存的核对记录"),)
    assert after.title == "用户手工保留的标题"
    assert after.incomplete == ((1, question),)


@pytest.mark.asyncio
async def test_explicit_retries_after_real_http_errors_keep_one_pending_original(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("retry", model="openai/gpt-4o-mini", language="en", title="保留人工标题")
    store.append_exchange("retry", "original", "人工保存的原始问题", "人工核对的历史记录")
    original = store.turns("retry")
    question = "  Read sources/fixture.md.\nPreserve this exact submitted question.  "
    config_path = root / ".ultrakb/config.yaml"
    save_config(
        config_path,
        {**load_config(config_path), "model_policy": {"max_calls": 3, "retries": 0, "backoff": 0}},
    )
    results = []
    with ErrorTransport(status=401) as service:
        (root / ".env").write_text(
            f"LLM_API_KEY=offline-error-test-key\nOPENAI_API_BASE={service.url}\n"
        )
        for retry in range(2):
            result = await continue_conversation(
                root,
                question,
                session_id="retry",
                attempt_id="same-original",
                submission_order=7,
                scope=storage_scope(root, identity),
            )
            results.append(result)
            assert result.status == "failed" and not result.answer, (result.status, result.error)
            assert len(service.requests) == retry + 1
            history = read_conversation(root, "retry", scope=storage_scope(root, identity))
            assert store.turns("retry") == original
            assert history.title == "保留人工标题"
            assert history.incomplete == ((1, question),)
    records = read_requests(root)
    assert len(records) == 2
    assert len({record.execution_id for record in records}) == 2
    assert all(record.send_status == "sent" and record.state == "failed" for record in records)
    assert all(record.input_total is None and record.output_total is None for record in records)
    assert [result.model_usage["current"]["actual_sends"] for result in results] == [1, 1]


@pytest.mark.asyncio
async def test_explicit_retry_keeps_the_original_submission_budget(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    path = root / ".ultrakb/config.yaml"
    save_config(
        path, {**load_config(path), "model_policy": {"max_calls": 2, "retries": 0, "backoff": 0}}
    )
    with ErrorTransport(status=503) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        for retry in range(3):
            result = await continue_conversation(
                root,
                "Keep this exact question.",
                new_session_id="bounded-retry",
                attempt_id="bounded-submission",
                scope=storage_scope(root, identity),
            )
            assert result.status != "completed"
            assert len(service.requests) == min(retry + 1, 2)
        assert result.model_usage["current"]["actual_sends"] == 0
    assert len(read_requests(root)) == 2
