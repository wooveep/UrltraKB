"""Measured progress crosses parser, worker history and live native UI boundaries."""

import json
import queue
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from openkb.progress import ProgressStep, progress_reporting, progress_scope, read_progress
from openkb.runtime.records import TaskView, UnitIdentity
from openkb.runtime.requests import ImportFile


def test_nested_wait_does_not_advance_parent_and_failure_keeps_position():
    events = []
    with progress_reporting(events.append):
        with pytest.raises(RuntimeError), progress_scope("docx", 10, "paragraphs") as parent:
            parent.advance(3)
            with progress_scope("pdf", 2, "pages") as child:
                child.advance()
                with progress_scope("cloud_ocr"):
                    snapshot = read_progress(events[-1]["progress"])
                    assert [step.percent for step in snapshot] == [30, 50, None]
                assert read_progress(events[-1]["progress"])[0].percent == 30
            raise RuntimeError("stop fixture")
    assert read_progress(events[-1]["progress"]) == (ProgressStep("docx", 3, 10, "paragraphs"),)
    with progress_reporting(events.append), progress_scope("pdf", 1, "pages") as next_work:
        next_work.advance()
    assert any(e["progress"] == [asdict(ProgressStep("pdf", 1, 1, "pages"))] for e in events)
    assert events[-1]["progress"] == []


@pytest.mark.parametrize(
    "row",
    [
        {"phase": "pdf", "completed": True, "total": 2},
        {"phase": "pdf", "completed": 3, "total": 2},
        {"phase": "pdf", "completed": -1, "total": 2},
        {"phase": "pdf", "total": float("nan")},
        {"phase": "untrusted label"},
    ],
)
def test_progress_boundary_rejects_invalid_counts(row):
    with pytest.raises(ValueError):
        read_progress([row])


def test_docx_attachment_progress_keeps_independent_totals(kb_dir, tmp_path):
    from openkb.inputs import prepared_input
    from openkb.parsing import parse_document
    from openkb.sources import SourceStore
    from tests.document_fixtures import write_docx
    from tests.docx_attachment_fixtures import attached_docx

    child = tmp_path / "child.docx"
    write_docx(child, "<w:p><w:r><w:t>First.</w:t></w:r></w:p>" * 3)
    parent = attached_docx(tmp_path / "parent.docx", child.read_bytes())
    with prepared_input(parent) as ready:
        source = SourceStore(kb_dir).intake(ready)
    events = []
    with progress_reporting(events.append):
        parsed = parse_document(kb_dir, source)
    snapshots = [read_progress(e["progress"]) for e in events]
    assert any(
        len(s) == 2
        and s[0] == ProgressStep("docx", 0, 1, "paragraphs")
        and s[1] == ProgressStep("docx", 3, 3, "paragraphs")
        for s in snapshots
    )
    assert (ProgressStep("docx", 1, 1, "paragraphs"),) in snapshots
    assert len(parsed.blocks) == 4


def test_pdf_counter_only_advances_after_page_result(kb_dir, tmp_path):
    import pymupdf

    from openkb.parsing_pdf import parse_pdf
    from openkb.sources import SourceStore

    path = tmp_path / "scanned.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.new_page()
        pdf.save(path)
    events, observed = [], []

    class Ocr:
        def page(self, document, page):
            with progress_scope("cloud_ocr"):
                observed.append(read_progress(events[-1]["progress"])[0])
            return [], "ocr_empty_page"

    with progress_reporting(events.append):
        _, quality = parse_pdf(path, SourceStore(kb_dir), ocr=Ocr())
    assert observed == [ProgressStep("pdf", 0, 2, "pages"), ProgressStep("pdf", 1, 2, "pages")]
    assert len(quality) == 2  # Inspection completed; the source still needs quality review.


def test_runtime_persists_progress_without_heartbeat_inflation(kb_dir, tmp_path):
    from openkb.runtime.tasks import TaskManager, _Task

    identity = UnitIdentity.create("b" * 32, 0, str(kb_dir), ImportFile(str(kb_dir / "source.pdf")))
    task = _Task(
        TaskView("b" * 32, str(kb_dir), "ImportFile", "running", "parsing", 1, (), False, False),
        identities=(identity,),
    )
    events = queue.Queue()
    attempt = SimpleNamespace(events=events, identity=identity, last_sequence=0)
    manager = TaskManager(history_dir=tmp_path / "history")
    try:
        for index, data in enumerate(
            [
                {"event": "progress", "progress": [asdict(ProgressStep("pdf", 2, 8, "pages"))]},
                {
                    "event": "diagnostic",
                    "text": "heartbeat",
                    "activity": False,
                    "at": "2026-09-10T00:00:00+00:00",
                },
                {"event": "progress", "progress": [{"phase": "pdf", "completed": 200, "total": 8}]},
            ],
            1,
        ):
            events.put({"identity": asdict(identity), "sequence": index, "data": data})
        with manager._condition:
            manager._progress(task, attempt)
        assert task.view.progress[0].percent == 25
        assert task.view.last_activity_at != "2026-09-10T00:00:00+00:00"
        saved = json.loads((manager.history_dir / f"{task.view.id}.json").read_text())
        restored = TaskView.from_summary(saved["view"])
        assert restored.progress == task.view.progress and restored.state == "interrupted"
        old = task.view.summary()
        old.pop("progress")
        assert TaskView.from_summary(old).progress == ()
    finally:
        manager.shutdown(stop=True)
        assert manager.join(5)


def test_compilation_counts_validated_source_text_and_generated_topics(
    kb_dir, tmp_path, model_service
):
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext

    file = tmp_path / "progress.md"
    file.write_text("# Limits\n\nThe timeout is 42 seconds.\n", encoding="utf-8")
    events = []
    result = import_document(kb_dir, file, context=ExecutionContext(on_event=events.append))
    assert result.status == "added"
    steps = [
        step
        for e in events
        if e.get("event") == "progress"
        for step in read_progress(e["progress"])
    ]
    for phase in ("text", "facts", "generation"):
        assert any(
            step.phase == phase and step.total and step.completed == step.total for step in steps
        )


def test_percentage_names_its_stage_and_does_not_claim_partial_success():
    pytest.importorskip("PySide6")
    from openkb.desktop.task_progress import progress_presentation

    task = TaskView(
        "a" * 32,
        "/kb",
        "ReparseSource",
        "running",
        "parsing",
        1,
        (),
        False,
        False,
        progress=(ProgressStep("docx", 37, 100, "paragraphs"), ProgressStep("cloud_ocr")),
    )
    percent, text, detail = progress_presentation(task)
    assert percent == 37 and "DOCX" in text and "37/100" in detail and "等待云端" in detail
    partial = replace(
        task, state="partial", progress=(ProgressStep("docx", 100, 100, "paragraphs"),)
    )
    assert "部分完成" in progress_presentation(partial)[1]
    assert "任务完成" not in progress_presentation(partial)[1]
    assert progress_presentation(replace(task, progress=()))[0] is None
