"""Conversation commit rules through real storage and local error transport."""

import pytest
from error_transport import ErrorTransport

from ultrakb import config
from ultrakb.agent.chat_session import ChatSession, load_session
from ultrakb.application.connection_capacity import template_capacity
from ultrakb.application.connection_templates import CONNECTION_TEMPLATES
from ultrakb.application.conversations import continue_conversation
from ultrakb.llm_usage import read_requests
from ultrakb.locks import atomic_write_text

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("assistant_text", ["", " \n\t"])
def test_unfinished_commit_keeps_pending_submission_and_authored_history(kb_dir, assistant_text):
    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    session.title = "用户手工保留的标题"
    session.record_turn("作者保存的原文问题", "作者保存的核对记录", [])
    original = session.turns[0]
    question = "  保留换行\n以及原始问题  "
    attempt = session.begin_attempt(question, identity="waiting-answer", submission_order=7)
    before = session.to_dict()
    saved = session.path.read_bytes()

    with pytest.raises(ValueError, match="committed exchange requires.*text"):
        session.record_turn(question, assistant_text, [], attempt_id=attempt)

    assert session.to_dict() == before
    assert session.path.read_bytes() == saved
    restored = load_session(kb_dir, session.id)
    assert restored.turns == [original]
    assert restored.title == "用户手工保留的标题"
    assert restored.incomplete == [
        {"id": attempt, "after_turn": 1, "message": question, "order": 7}
    ]


@pytest.mark.asyncio
async def test_explicit_retries_after_real_http_errors_keep_one_pending_original(
    kb_dir, tmp_path, monkeypatch, record_property
):
    global_dir = tmp_path / "isolated-global"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", global_dir)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", global_dir / "global.yaml")
    for name in ("LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    atomic_write_text(kb_dir / "wiki/index.md", "# Knowledge\nSee sources/manual.md.")
    atomic_write_text(kb_dir / "wiki/sources/manual.md", "An authored original measurement.")

    session = ChatSession.new(kb_dir, "deepseek/deepseek-flash", "en")
    session.title = "保留人工标题"
    session.record_turn("人工保存的原始问题", "人工核对的历史记录", [])
    original = session.turns[0]
    question = "  Read sources/manual.md.\nPreserve this exact submitted question.  "
    attempt = "retry-the-original-submission"
    results = []
    with ErrorTransport(status=401) as service:
        config.save_config(
            kb_dir / ".ultrakb/config.yaml",
            {
                "model": "deepseek/deepseek-flash",
                "language": "en",
                "model_capacity": template_capacity(CONNECTION_TEMPLATES[2]),
                "model_policy": {"max_calls": 3, "retries": 0, "backoff": 0},
                "role_generation": {"answer": {"max_tokens": 128, "reasoning_effort": "none"}},
            },
        )
        atomic_write_text(
            kb_dir / ".env",
            f"LLM_API_KEY=offline-error-test-key\nOPENAI_API_BASE={service.url}\n",
        )
        for explicit_retry in range(2):
            result = await continue_conversation(
                kb_dir,
                question,
                session_id=session.id,
                attempt_id=attempt,
                submission_order=7,
            )
            results.append(result)
            assert result.status == "failed" and not result.answer
            assert len(service.requests) == explicit_retry + 1
            restored = load_session(kb_dir, session.id)
            assert restored.turn_count == 1
            assert restored.turns == [original]
            assert restored.incomplete == [
                {"id": attempt, "after_turn": 1, "message": question, "order": 7}
            ]
            if explicit_retry == 0:
                restored.title = "两次尝试之间人工修改的标题"
                restored.save()
            else:
                assert restored.title == "两次尝试之间人工修改的标题"
        assert all(request["path"] == "/v1/chat/completions" for request in service.requests)
        record_property("http_sends", len(service.requests))
        record_property("successful_model_responses", 0)

    records = read_requests(kb_dir)
    assert len(records) == 2
    assert len({record.execution_id for record in records}) == 2
    assert all(record.send_status == "sent" and record.state == "failed" for record in records)
    assert all(record.usage_status == "unknown" for record in records)
    assert all(record.input_total is None and record.output_total is None for record in records)
    assert [result.model_usage["current"]["requests"] for result in results] == [1, 1]
    assert [result.model_usage["current"]["unknown_requests"] for result in results] == [1, 1]
