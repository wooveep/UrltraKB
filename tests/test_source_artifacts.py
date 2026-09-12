"""Stage views read saved work without compiling or treating drafts as publication."""

import json

import litellm
import pytest

from openkb.application.documents import import_document
from openkb.application.source_artifacts import compilation_artifacts, published_source_pages
from openkb.application.source_history import source_status
from openkb.processing import ProcessingIncomplete
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.fixture
def source_run(kb_dir, tmp_path, monkeypatch):
    calls = []

    def create(*, stop=False):
        def completion(**kwargs):
            payload = json.loads(kwargs["messages"][-1]["content"])
            calls.append(payload["stage"])
            if stop and payload["stage"] == "verification":
                raise ProcessingIncomplete("request_timeout", "generation")
            return response(evidence_response(payload))

        monkeypatch.setattr(litellm, "completion", completion)
        file = tmp_path / ("待完成手册.md" if stop else "已完成手册.md")
        file.write_text(
            "Required pressure: 37 kPa.\n\nNever retry authentication failure.", encoding="utf-8"
        )
        result = import_document(kb_dir, file)
        return result, source_status(kb_dir, result.source_id), calls

    return create


def read_stage(kb, result, stage, **kwargs):
    return compilation_artifacts(
        kb, result.source_id, result.input_version, result.parse_id, stage, **kwargs
    )


def test_saved_facts_plans_and_verified_pages_are_read_without_model_calls(kb_dir, source_run):
    result, _, calls = source_run()
    assert result.knowledge_compilation == "completed", result
    before = list(calls)
    facts = read_stage(kb_dir, result, "facts")
    assert facts["total"] and "37 kPa" in facts["records"][0]["text"]
    assert read_stage(kb_dir, result, "planning")["total"]
    pages = read_stage(kb_dir, result, "generation")
    assert pages["total"] and all(not r["draft"] for r in pages["records"])
    assert published_source_pages(kb_dir, result.source_id, result.input_version)
    assert calls == before


def test_draft_is_visible_but_is_not_an_entered_knowledge_page(kb_dir, source_run):
    result, _, calls = source_run(stop=True)
    assert result.reason == "request_timeout"
    before = list(calls)
    records = read_stage(kb_dir, result, "generation")
    assert records["total"] == 1 and records["records"][0]["draft"]
    assert "尚未通过校验" in records["records"][0]["text"]
    assert published_source_pages(kb_dir, result.source_id, result.input_version) == []
    assert calls == before


def test_artifact_preview_validates_digest_and_version(kb_dir, source_run):
    result, _, _ = source_run()
    with pytest.raises(ValueError, match="identit"):
        compilation_artifacts(kb_dir, "b" * 32, result.input_version, result.parse_id, "facts")
    record = read_stage(kb_dir, result, "facts")["records"][0]
    path = kb_dir / ".openkb/source-store/compilation" / (record["key"] + ".json")
    value = json.loads(path.read_text())
    value["value"]["units"][0]["facts"][0]["statement"] = "tampered"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="digest"):
        read_stage(kb_dir, result, "facts")


def test_artifact_pages_reject_invalid_bounds_and_do_not_repeat_records(kb_dir, source_run):
    result, _, _ = source_run()
    with pytest.raises(ValueError, match="page"):
        read_stage(kb_dir, result, "facts", offset=-1)
    first = read_stage(kb_dir, result, "facts", limit=1)
    second = read_stage(kb_dir, result, "facts", offset=1, limit=1)
    assert {r["key"] for r in first["records"]}.isdisjoint(r["key"] for r in second["records"])
