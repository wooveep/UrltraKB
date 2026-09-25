"""Provider JSON recovery through the actual document planning boundary."""

import asyncio
import json
from types import SimpleNamespace

import pytest
import yaml

from openkb.agent.compiler import _llm_call, _llm_call_async
from openkb.agent.document_json_response import classify_json_response
from openkb.application.documents import import_document
from openkb.execution_receipt import ModelText, derived_text
from openkb.processing import DEFAULT_PROCESSING, processing_scope
from tests.http_model_fixture import ModelReply, evidence_response


def test_syntax_repair_authorizes_wire_ids_before_one_decode(kb_dir, tmp_path, model_service):
    source = tmp_path / "syntax.md"
    source.write_text("A small procedure uses one source paragraph.")
    original = None
    modes = []

    def respond(body):
        nonlocal original
        request = json.loads(body["messages"][-1]["content"])
        assert request["stage"] == "planning"
        modes.append(request["response_mode"])
        if original is None:
            original = json.dumps(evidence_response(request), ensure_ascii=False)
            return ModelReply(original + "}")
        assert request["repair_request"]
        return ModelReply(original)

    model_service.respond = respond
    result = import_document(kb_dir, source, plan_only=True)

    assert result.reason == "document_plan_ready"
    assert modes == ["plan", "plan"]
    assert len(model_service) == 2


@pytest.mark.parametrize("empty", [None, "", " \n\t "])
def test_completed_empty_initial_plan_resends_same_request(kb_dir, tmp_path, model_service, empty):
    source = tmp_path / "empty-plan.md"
    source.write_text("A small source statement.")
    requests = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        assert request["stage"] == "planning"
        requests.append(request)
        if len(requests) == 1:
            return ModelReply(empty)
        return evidence_response(request)

    model_service.respond = respond
    result = import_document(kb_dir, source, plan_only=True)

    assert result.reason == "document_plan_ready"
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[1]["response_mode"] == "plan"


def test_two_completed_empty_plans_stop_without_third_request(kb_dir, tmp_path, model_service):
    source = tmp_path / "still-empty.md"
    source.write_text("A small source statement.")
    model_service.respond = lambda _body: ModelReply(None)

    result = import_document(kb_dir, source, plan_only=True)

    assert result.reason == "document_plan_empty"
    assert any(row["reason"] == "document_plan_empty_response" for row in result.omissions)
    assert len(model_service) == 2
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_empty_syntax_answer_keeps_rejected_wire_candidate(kb_dir, tmp_path, model_service):
    config_path = kb_dir / ".openkb/config.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["processing"]["max_attempts"] = 3
    config_path.write_text(yaml.safe_dump(config))
    source = tmp_path / "empty-syntax.md"
    source.write_text("A short procedure.")
    requests = []
    valid_wire = None

    def respond(body):
        nonlocal valid_wire
        request = json.loads(body["messages"][-1]["content"])
        requests.append(request)
        if len(requests) == 1:
            valid_wire = json.dumps(evidence_response(request))
            return ModelReply(valid_wire + "}")
        if len(requests) == 2:
            return ModelReply(None)
        return ModelReply(valid_wire)

    model_service.respond = respond
    result = import_document(kb_dir, source, plan_only=True)

    assert result.reason == "document_plan_ready"
    assert len(requests) == 3
    assert requests[1] == requests[2]
    assert requests[1]["repair_request"]["rejected_candidate"] == valid_wire + "}"


def test_empty_syntax_answer_exhausts_shared_attempts(kb_dir, tmp_path, model_service):
    source = tmp_path / "exhausted-syntax.md"
    source.write_text("A short procedure.")
    requests = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        requests.append(request)
        if len(requests) == 1:
            return ModelReply(json.dumps(evidence_response(request)) + "}")
        return ModelReply(" ")

    model_service.respond = respond
    result = import_document(kb_dir, source, plan_only=True)

    assert result.reason == "document_plan_empty"
    assert any(row["reason"] == "document_plan_empty_response" for row in result.omissions)
    assert len(requests) == 2
    assert requests[1]["repair_request"]["mode"] == "syntax_repair"


@pytest.mark.parametrize("content", ["null", '""', "[]", "{}"])
def test_nonempty_wrong_top_level_is_not_transport_empty(kb_dir, tmp_path, model_service, content):
    source = tmp_path / "wrong-top-level.md"
    source.write_text("A short procedure.")
    model_service.respond = lambda _body: ModelReply(content)

    result = import_document(kb_dir, source, plan_only=True)

    assert result.reason != "document_plan_empty_response"
    assert len(model_service) <= 2


@pytest.mark.parametrize(
    "text,finish,expected",
    [
        ("", "stop", "empty_content"),
        (" \n", "stop", "empty_content"),
        ('{"decisions":[]}', "stop", "content"),
        ("", "length", "length"),
        ("{}", "content_filter", "invalid_finish"),
        ("", None, "invalid_finish"),
    ],
)
def test_completed_response_classification(text, finish, expected):
    wire = ModelText(text, 128, raw_content=text, finish_reason=finish, representation="wire")
    assert classify_json_response(wire) == expected
    assert derived_text(text, wire).representation == "derived"
    assert derived_text(text, wire).raw_content is None


def test_default_and_raw_llm_call_modes_are_symmetric(monkeypatch):
    import litellm

    seen = []

    def response(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=' {"key":"@e:a"} '),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
        )

    monkeypatch.setattr(litellm, "completion", response)

    async def async_response(**kwargs):
        return response(**kwargs)

    monkeypatch.setattr(litellm, "acompletion", async_response)

    class Messages(list):
        def decode_response(self, value):
            return value.replace("@e:a", "expanded-id")

    messages = Messages([{"role": "user", "content": "JSON"}])
    settings = {
        "model": "openai/offline-test",
        "processing": {
            **DEFAULT_PROCESSING,
            "max_context_tokens": 128_000,
            "max_output_tokens": 4_096,
        },
    }
    with processing_scope(settings):
        sync_default = _llm_call("openai/offline-test", messages, "planning")
        sync_raw = _llm_call("openai/offline-test", messages, "planning", decode_response=False)
        async_default = asyncio.run(_llm_call_async("openai/offline-test", messages, "planning"))
        async_raw = asyncio.run(
            _llm_call_async("openai/offline-test", messages, "planning", decode_response=False)
        )
    assert sync_default == async_default == '{"key":"expanded-id"}'
    assert sync_raw == async_raw == ' {"key":"@e:a"} '
    assert sync_raw.raw_content == sync_raw
    assert async_raw.representation == "wire"
    assert all("decode_response" not in request for request in seen)
