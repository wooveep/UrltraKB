"""Stage views read saved work without compiling or treating drafts as publication."""

import json
import sqlite3

import litellm
import pytest

from openkb.agent.checkpoint_artifacts import artifact_summary
from openkb.agent.compilation_index import index_path, update_index
from openkb.application.documents import import_document
from openkb.application.source_artifacts import (
    _formally_verified_indexed_candidate,
    compilation_artifacts,
    published_source_pages,
)
from openkb.application.source_history import source_status
from openkb.processing import ProcessingIncomplete
from openkb.sources import SourceStore, content_id
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


def test_saved_document_plans_and_verified_pages_are_read_without_model_calls(kb_dir, source_run):
    result, _, calls = source_run()
    assert result.knowledge_compilation == "completed", result
    before = list(calls)
    plans = read_stage(kb_dir, result, "planning")
    assert plans["total"] and "Notes" in plans["records"][0]["text"]
    pages = read_stage(kb_dir, result, "generation")
    assert pages["total"] and all(not r["draft"] for r in pages["records"])
    assert published_source_pages(kb_dir, result.source_id, result.input_version)
    assert calls == before


def test_verification_receipt_exposes_its_reason_in_the_generation_stage(kb_dir, source_run):
    result, _, _ = source_run()

    receipts = read_stage(kb_dir, result, "verification")
    assert receipts["total"]
    assert (
        "理由：The controlled contribution matches its evidence." in receipts["records"][0]["text"]
    )

    generation = read_stage(kb_dir, result, "generation")
    assert any(
        record["stage"] == "verification" and "理由：" in record["text"]
        for record in generation["records"]
    )


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
        compilation_artifacts(kb_dir, "b" * 32, result.input_version, result.parse_id, "planning")
    record = read_stage(kb_dir, result, "planning")["records"][0]
    path = kb_dir / ".openkb/source-store/compilation" / (record["key"] + ".json")
    value = json.loads(path.read_text())
    value["value"] = "tampered"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="digest"):
        read_stage(kb_dir, result, "planning")


def test_artifact_preview_requires_the_linked_review_checkpoint(kb_dir, source_run):
    result, _, _ = source_run()
    path = next((kb_dir / ".openkb/source-store/compilation/recovery").glob("*-draft.json"))
    record = json.loads(path.read_text())
    review = record["value"]["output"]["review_receipt"]["reviews"][0]
    review["checkpoint"] = "0" * 64
    record["digest"] = content_id(record["value"])
    path.write_text(json.dumps(record))

    preview = read_stage(kb_dir, result, "generation")["records"][0]
    assert preview["draft"]
    assert "尚未通过校验" in preview["text"]


def test_artifact_preview_requires_every_legacy_fragment_review_receipt():
    """Historic fragment receipts remain formal only when every link validates."""

    first, second = "a" * 64, "b" * 64
    accepted = {
        ("page", first): {"checkpoint": "c" * 64, "result": "r" * 64},
        ("page", second): {"checkpoint": "d" * 64, "result": "s" * 64},
    }
    row = {
        "page_key": "page",
        "candidate": "whole-candidate",
        "adopted": True,
        "reviews": [
            {
                "verdict": "supported",
                "candidate": first,
                "checkpoint": "c" * 64,
                "result": "r" * 64,
            },
            {
                "verdict": "advisory",
                "candidate": second,
                "checkpoint": "d" * 64,
                "result": "s" * 64,
            },
        ],
        "review_fragments": [{"candidate": first}, {"candidate": second}],
    }

    assert _formally_verified_indexed_candidate(row, accepted)
    row["reviews"][1]["result"] = "forged"
    assert not _formally_verified_indexed_candidate(row, accepted)


def test_artifact_preview_streams_a_legacy_index_without_compact_summaries(kb_dir, source_run):
    result, _, _ = source_run()
    store = SourceStore(kb_dir)
    latest = kb_dir / ".openkb/source-store/compilation/latest" / (result.input_version + ".json")
    index_path(store, result.input_version).unlink()
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(
        json.dumps(
            {
                "checkpoints": [
                    path.stem
                    for path in (kb_dir / ".openkb/source-store/compilation").glob("*.json")
                ]
            }
        )
    )

    planning = read_stage(kb_dir, result, "planning")
    generation = read_stage(kb_dir, result, "generation")
    assert planning["total"] and "Notes" in planning["records"][0]["text"]
    assert generation["total"] and all(not row["draft"] for row in generation["records"])


def test_planning_progress_preview_is_hydrated_from_the_compact_index(kb_dir, source_run):
    result, _, _ = source_run()
    recovery = kb_dir / ".openkb/source-store/compilation/recovery"
    template = json.loads(next(recovery.glob("*-plan.json")).read_text())
    key = "f" * 64
    progress = {
        "protocol": "document-plan-ledger-v1",
        "ledger": "e" * 64,
        "status": "pending",
        "completed_windows": 1,
        "accepted_window_ids": ["first-window"],
        "window_count": 2,
        "state_digest": "d" * 64,
        "preview": {
            "overview": {
                "text": "Partial cumulative overview",
                "ranges": [[0, 1]],
                "limitations": [],
                "status": "partial",
                "range_count": 1,
                "ranges_truncated": False,
                "text_truncated": False,
            },
            "pages": [
                {
                    "name": "concepts/first",
                    "title": "First",
                    "subject_ranges": [[0, 1]],
                    "state": "ready",
                    "quality": "planned",
                }
            ],
            "source_only": [],
            "unresolved": [],
            "counts": {"pages": 1, "source_only": 0, "unresolved": 0, "open_unresolved": 0},
            "truncated": {
                "overview": False,
                "pages": False,
                "source_only": False,
                "unresolved": False,
            },
        },
    }
    record = {
        "input": template["input"],
        "key": key,
        "kind": "plan",
        "value": progress,
        "digest": content_id(progress),
    }
    (recovery / f"{key}-plan.json").write_text(json.dumps(record))
    update_index(
        SourceStore(kb_dir), result.input_version, summary=artifact_summary(record, storage="plan")
    )

    planning = read_stage(kb_dir, result, "planning")
    preview = next(row for row in planning["records"] if row["key"] == key)
    assert "累计进度：已接受 1/2 个目标" in preview["text"]
    assert "Partial cumulative overview" in preview["text"]


def test_artifact_pages_reject_invalid_bounds_and_do_not_repeat_records(kb_dir, source_run):
    result, _, _ = source_run()
    with pytest.raises(ValueError, match="page"):
        read_stage(kb_dir, result, "planning", offset=-1)
    first = read_stage(kb_dir, result, "planning", limit=1)
    second = read_stage(kb_dir, result, "planning", offset=1, limit=1)
    assert {r["key"] for r in first["records"]}.isdisjoint(r["key"] for r in second["records"])


def test_artifact_pagination_does_not_materialize_all_historical_rows(
    kb_dir, source_run, monkeypatch
):
    """The UI hydrates only the selected SQLite summary, even deep in history."""

    result, _, _ = source_run()
    store = SourceStore(kb_dir)
    path = index_path(store, result.input_version)
    with sqlite3.connect(path) as db, db:
        db.execute("DELETE FROM artifacts")
        rows = []
        for number in range(10_000):
            key = f"{number:064x}"
            summary = {
                "schema": 1,
                "storage": "checkpoint",
                "key": key,
                "stage": "planning",
                "source": result.source_id,
                "version": result.input_version,
                "parse": result.parse_id,
                "model": "openai/offline",
                "draft": False,
                "adopted": False,
            }
            rows.append(
                (
                    "checkpoint:" + key,
                    result.source_id,
                    result.input_version,
                    result.parse_id,
                    "checkpoint",
                    key,
                    "planning",
                    0,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    json.dumps(summary, separators=(",", ":"), sort_keys=True),
                )
            )
        db.executemany(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )

    import openkb.application.source_artifacts as module

    hydrated = []

    def selected_only(_store, summary, *_identity):
        hydrated.append(summary["key"])
        return {
            "key": summary["key"],
            "value": {"overview": {}, "page_changes": []},
            "draft": False,
        }

    monkeypatch.setattr(module, "load_compilation_artifact", selected_only)
    page = read_stage(kb_dir, result, "planning", offset=9_999, limit=1)

    assert page["total"] == 10_000
    assert hydrated == [f"{9_999:064x}"]
    assert [record["key"] for record in page["records"]] == hydrated
