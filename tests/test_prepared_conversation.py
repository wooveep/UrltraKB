"""Prepared user intent uses native originals and the actual SDK input boundary."""

import json

import pytest
from native_usage_fixtures import read_requests
from stream_transport import StreamTransport
from test_storage_questions import question_knowledge

from ultrakb.application.conversations import continue_conversation, read_conversation
from ultrakb.application.execution import ExecutionContext
from ultrakb.application.views import storage_scope
from ultrakb.config import LlmCredentialBundle, load_config, save_config
from ultrakb.storage.session_store import SessionStore


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [False, True])
async def test_unknown_or_exceeded_capacity_stops_before_send_and_keeps_question(tmp_path, known):
    root, identity, _ = question_knowledge(tmp_path)
    path = root / ".ultrakb/config.yaml"
    config = load_config(path)
    config.pop("model_capacity", None)
    config["model"] = "openai/private-chat"
    if known:
        from tokenizers import Tokenizer, models, pre_tokenizers

        tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "current": 1}, unk_token="[UNK]"))
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        local = root / "fixture-tokenizer.json"
        tokenizer.save(str(local))
        config["model_capacity"] = {"max_input_tokens": 16, "tokenizer_model": f"local:{local}"}
    save_config(path, config)
    question = "current " * 40
    result = await continue_conversation(
        root,
        question,
        context=ExecutionContext(credential_bundle=LlmCredentialBundle(api_key="unused")),
        scope=storage_scope(root, identity),
    )
    assert result.status == "blocked", result.error
    assert "capacity" in result.error.lower()
    history = read_conversation(root, result.session_id, scope=storage_scope(root, identity))
    assert history.turns == () and history.incomplete == ((0, question),)
    assert read_requests(root) == []


@pytest.mark.asyncio
async def test_recent_input_uses_six_real_user_turns_and_preserves_full_conversation(tmp_path):
    root, identity, _ = question_knowledge(tmp_path)
    store = SessionStore(root, identity)
    store.create("recent", model="openai/gpt-4o-mini", language="en")
    for index in range(8):
        store.append_exchange(
            "recent",
            f"turn-{index}",
            f"USER_INTENT_{index}: indoor room.",
            "ASSISTANT_POISON",
            metadata={"tool_note": "TOOL_POISON"},
        )
    original = store.turns("recent")
    question = "State my preferred room."
    with StreamTransport([{"content": "An indoor room."}]) as service:
        (root / ".env").write_text(f"LLM_API_KEY=offline-test-key\nOPENAI_API_BASE={service.url}\n")
        result = await continue_conversation(
            root, question, session_id="recent", scope=storage_scope(root, identity)
        )
    assert result.status == "completed", result.error
    assert len(service.requests) == 1
    sent = service.requests[0]["messages"]
    assert [m["role"] for m in sent if m["role"] != "system"] == ["user"]
    text = json.dumps(sent)
    assert "ASSISTANT_POISON" not in text and "TOOL_POISON" not in text
    assert "USER_INTENT_0" not in text and "USER_INTENT_1" not in text
    assert all(f"USER_INTENT_{index}" in text for index in range(2, 8))
    assert all(turn["turn_id"] in text for turn in original[-6:])
    restored = store.turns("recent")
    assert restored[:-1] == original
    assert restored[-1]["messages"] == [
        {"role": "user", "content": question},
        {"role": "assistant", "content": result.answer},
    ]
    assert result.context["recall_reason"] == "below_recall_threshold"


@pytest.mark.asyncio
async def test_current_question_fits_but_reader_schemas_exceed_capacity(tmp_path):
    from ultrakb.application.conversations import ask_question

    root, identity, _ = question_knowledge(tmp_path)
    path = root / ".ultrakb/config.yaml"
    save_config(
        path,
        {
            **load_config(path),
            "model_capacity": {"max_input_tokens": 16, "tokenizer_model": "gpt-4o-mini"},
        },
    )
    result = await ask_question(
        root,
        "Explain.",
        context=ExecutionContext(credential_bundle=LlmCredentialBundle(api_key="unused")),
        scope=storage_scope(root, identity),
    )
    assert result.status == "blocked", result.error
    assert "capacity" in result.error.lower()
    assert read_requests(root) == []


@pytest.mark.asyncio
async def test_threshold_recall_rereads_old_users_and_later_correction_wins(
    kb_dir, model_service, monkeypatch
):
    from ultrakb.application.views import published_scope

    monkeypatch.setattr("ultrakb.context.preparation.RECALL_THRESHOLD_TOKENS", 1)
    path = kb_dir / ".ultrakb/config.yaml"
    save_config(
        path,
        {
            **load_config(path),
            "model": "deepseek/deepseek-flash",
            "model_capacity": {"max_input_tokens": 65536, "tokenizer_model": "gpt-4o-mini"},
            "model_policy": {"max_calls": 24, "retries": 0},
        },
    )
    store = SessionStore(kb_dir, published_scope(kb_dir).knowledge.identity)
    store.create("corrected", model="deepseek/deepseek-flash", language="en")
    for index in range(9):
        text = (
            "My room is ARCHIVE_ROOM and my budget is 42 credits."
            if index == 0
            else "Correction: choose GARDEN_ROOM; keep my original budget."
            if index == 8
            else f"Workspace notes {index}."
        )
        store.append_exchange("corrected", f"turn-{index}", text, "ASSISTANT_POISON: budget is 999")
    result = await continue_conversation(
        kb_dir, "Confirm my room and budget.", session_id="corrected"
    )
    assert result.status == "completed", result.error
    assert "GARDEN_ROOM" in result.answer and "42" in result.answer and "999" not in result.answer
    sent = json.dumps(model_service.requests)
    assert "ASSISTANT_POISON" not in sent
    assert all(term in sent for term in ("ARCHIVE_ROOM", "GARDEN_ROOM", "42 credits"))
    assert result.context["recall"]["status"] == "completed"
    assert 0 < result.context["recall"]["context_tokens"] <= 2000
    assert len(store.turns("corrected")) == 10
    assert read_requests(kb_dir)
