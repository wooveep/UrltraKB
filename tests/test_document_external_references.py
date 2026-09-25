"""External mentions stay tied to one saved plan and exact source bytes."""

import hashlib
import json
from types import SimpleNamespace

import litellm
import pytest

from openkb.agent.document_external_references import (
    iter_external_references,
    plan_reference,
)
from openkb.agent.document_orchestrator import plan_document
from openkb.agent.document_plan import DocumentPlan, to_dict
from openkb.agent.document_plan_annotations import ExternalReference
from openkb.agent.document_planning_support import fallback_read_evidence
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.evidence import ParseStore
from openkb.processing import DEFAULT_PROCESSING
from openkb.sources import SourceStore, content_id
from tests.test_adaptive_processing import response
from tests.test_document_orchestrator import _DummyParsed, _DummySource


def test_reader_requires_explicit_plan_digest_and_original_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "openkb.agent.document_plan_proof_reader.verify_accepted_plan", lambda *_: None
    )
    body = b"Read Guide A."
    blob = hashlib.sha256(body).hexdigest()
    store = SourceStore(tmp_path)
    asset = store.root / "blobs" / blob[:2] / blob
    asset.parent.mkdir(parents=True)
    asset.write_bytes(body)
    parsed = SimpleNamespace(blocks=[SimpleNamespace(id="d" * 64, chars=len(body), blob=blob)])
    monkeypatch.setattr(ParseStore, "load", lambda self, key: parsed)
    plan = DocumentPlan(
        metadata={
            "protocol": "document-plan-v2",
            "recovery_key": "a" * 64,
            "source_id": "a" * 32,
            "version_id": "b" * 64,
            "parse_id": "c" * 64,
        },
        external_references=[
            ExternalReference(
                key="xref:" + "e" * 64,
                location=[{"block_index": 0, "start_char": 0, "end_char": 12}],
                raw_quote="Read Guide A",
                target_document="Guide A",
            )
        ],
    )
    value = to_dict(plan)
    path = store.root / "compilation" / "recovery" / ("a" * 64 + "-plan.json")
    path.parent.mkdir(parents=True)
    record = {
        "key": "a" * 64,
        "kind": "plan",
        "value": value,
        "digest": content_id(value),
        "input": {"source": "a" * 32, "version": "b" * 64, "parse": "c" * 64},
    }
    path.write_text(json.dumps(record))
    reference = plan_reference(plan)
    views = list(iter_external_references(tmp_path, reference))
    assert len(views) == 1
    assert views[0].status == "unlinked"
    assert views[0].reference.raw_quote == "Read Guide A"
    with pytest.raises(ValueError, match="plan record"):
        list(iter_external_references(tmp_path, {**reference, "plan_digest": "f" * 64}))
    record["value"]["external_references"][0]["raw_quote"] = "Read Guide B"
    record["digest"] = content_id(record["value"])
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="plan record"):
        list(iter_external_references(tmp_path, reference))
    with pytest.raises(ValueError, match="quote differs"):
        list(iter_external_references(tmp_path, {**reference, "plan_digest": record["digest"]}))
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="plan record"):
        list(iter_external_references(tmp_path, reference))


def test_reader_requires_immutable_markdown_reference_proof(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _DummyParsed(1)
    body = "See Guide A."
    block = parsed.blocks[0]
    block.text, block.chars = body, len(body)
    blob = hashlib.sha256(body.encode()).hexdigest()
    block.blob = blob
    store = SourceStore(tmp_path)
    asset = store.root / "blobs" / blob[:2] / blob
    asset.parent.mkdir(parents=True)
    asset.write_text(body)
    monkeypatch.setattr(ParseStore, "load", lambda self, key: parsed)
    monkeypatch.setattr(litellm, "token_counter", lambda **_: 100)
    monkeypatch.setattr(
        "openkb.agent.document_planning_support.read_target_evidence",
        lambda _kb, _source, _parsed, descriptor, _ranges: fallback_read_evidence(
            source, parsed, descriptor["start"], descriptor["end"]
        ),
    )
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    settings = {
        "model": "mock-model",
        "processing": {
            **DEFAULT_PROCESSING,
            "context_tokens": 128_000,
            "max_context_tokens": 128_000,
        },
    }

    def respond(messages, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        if payload["subtask"] == "overview":
            return "The source mentions Guide A."
        return "- Name: Guide note\n  Kind: concept"

    monkeypatch.setattr(
        litellm,
        "completion",
        lambda **kwargs: response(respond(kwargs["messages"]), tokens=10),
    )
    events = []
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            plan_only=True,
            on_event=events.append,
        )
    assert isinstance(plan, DocumentPlan), events
    assert plan.external_references, events
    handle = plan_reference(plan)
    assert len(list(iter_external_references(tmp_path, handle))) == 1
    state_path = store.root / "compilation" / "recovery" / (
        plan.metadata["recovery_key"] + "-markdown_plan.json"
    )
    state_record = json.loads(state_path.read_text())
    state_record["value"]["external_references"][0]["target_document"] = "Guide B"
    state_record["digest"] = content_id(state_record["value"])
    state_path.write_text(json.dumps(state_record))
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        resumed = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            plan_only=True,
            resume=True,
        )
    assert isinstance(resumed, DocumentPlan)
    assert [row.target_document for row in resumed.external_references] == ["Guide A"]
    handle = plan_reference(resumed)
    plan_path = store.root / "compilation" / "recovery" / (
        plan.metadata["recovery_key"] + "-plan.json"
    )
    record = json.loads(plan_path.read_text())
    original = json.dumps(record)
    record["value"]["external_references"] = []
    record["digest"] = content_id(record["value"])
    plan_path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="reference proof"):
        list(iter_external_references(tmp_path, {**handle, "plan_digest": record["digest"]}))
    plan_path.write_text(original)
    checkpoint = store.root / "compilation" / (
        resumed.metadata["reference_proof_key"] + ".json"
    )
    checkpoint.unlink()
    with pytest.raises(ValueError, match="Markdown plan reference proof"):
        list(iter_external_references(tmp_path, handle))
