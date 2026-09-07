"""Knowledge checks preserve committed fixes and report incomplete checks."""

import asyncio
from pathlib import Path

import pytest


def test_structural_check_repairs_links_and_saves_a_report(kb_dir):
    from openkb.application.execution import ExecutionContext
    from openkb.application.maintenance import LintOptions, check_knowledge

    page = kb_dir / "wiki/concepts/topic.md"
    page.write_text("---\ntype: Concept\ndescription: Topic\n---\n# Topic\n[[concepts/missing]]")
    context = ExecutionContext()
    result = asyncio.run(
        check_knowledge(kb_dir, LintOptions(fix=True, semantic=False), context=context)
    )
    assert result.status == "completed"
    assert result.files_changed == result.ghosts_removed == 1
    assert "[[concepts/missing]]" not in page.read_text()
    assert "type: Concept" in page.read_text()
    assert "updated: wiki/concepts/topic.md" in result.changes

    assert "No broken links found" in result.structural_report
    assert Path(result.report_path).is_file()
    assert result.report_path in result.resources
    assert context.snapshot is not None
    assert not result.unfinished


def test_repair_diagnostics_can_read_pages_without_replaying_journals(kb_dir):
    import json

    from openkb.application.repair import inspect_knowledge_base, read_diagnostic_page

    marker = kb_dir / ".openkb/needs-repair.json"
    marker.write_text('{"error_type":"MissingBackup"}')
    journal = kb_dir / ".openkb/journal/broken.json"
    journal.parent.mkdir(exist_ok=True)
    journal.write_text(json.dumps({"status": "active", "operation": "import", "entries": []}))
    page = kb_dir / "wiki/concepts/retained.md"
    page.write_text("# Retained manual page")
    before = marker.read_bytes(), journal.read_bytes(), page.read_bytes()
    diagnostic = inspect_knowledge_base(kb_dir)
    assert diagnostic.needs_repair
    assert "concepts/retained.md" in diagnostic.pages
    assert read_diagnostic_page(kb_dir, "concepts/retained.md") == "# Retained manual page"
    assert (marker.read_bytes(), journal.read_bytes(), page.read_bytes()) == before


def test_semantic_failure_keeps_link_fixes_and_an_explicitly_incomplete_report(kb_dir, monkeypatch):
    from agents import Runner

    from openkb.application.execution import ExecutionContext
    from openkb.application.maintenance import LintOptions, check_knowledge

    (kb_dir / ".openkb/hashes.json").write_text('{"document": {}}')
    page = kb_dir / "wiki/concepts/topic.md"
    page.write_text("# Topic\n[[concepts/missing]]")

    async def unavailable(*args, **kwargs):
        raise ConnectionError("private provider detail")

    monkeypatch.setattr(Runner, "run", unavailable)
    result = asyncio.run(check_knowledge(kb_dir, LintOptions(fix=True), context=ExecutionContext()))
    assert result.status == "completed"
    assert result.quality == ("semantic_lint_failed",)
    assert result.unfinished == ("semantic_lint",)
    assert result.files_changed == 1
    assert "[[concepts/missing]]" not in page.read_text()
    report = Path(result.report_path).read_text()
    assert "Knowledge lint failed" in report
    assert "private provider detail" not in report
    assert "updated: wiki/concepts/topic.md" in result.changes


def test_report_write_failure_preserves_already_committed_link_repairs(kb_dir):
    from openkb.application.maintenance import LintOptions, check_knowledge

    page = kb_dir / "wiki/concepts/topic.md"
    page.write_text("# Topic\n[[concepts/missing]]")
    reports = kb_dir / "wiki/reports"
    if reports.is_dir():
        reports.rmdir()
    reports.write_text("Existing non-directory resource")
    result = asyncio.run(check_knowledge(kb_dir, LintOptions(fix=True, semantic=False)))
    assert result.status == "failed"
    assert result.unfinished == ("report",)
    assert result.files_changed == 1
    assert "updated: wiki/concepts/topic.md" in result.changes
    assert "[[concepts/missing]]" not in page.read_text()
    assert reports.read_text() == "Existing non-directory resource"


@pytest.mark.parametrize("output", [None, "", " \n "])
def test_empty_semantic_output_is_a_saved_but_incomplete_check(kb_dir, monkeypatch, output):
    from types import SimpleNamespace

    from agents import Runner

    from openkb.application.execution import ExecutionContext
    from openkb.application.maintenance import check_knowledge

    (kb_dir / ".openkb/hashes.json").write_text('{"document": {}}')

    async def no_report(*args, **kwargs):
        return SimpleNamespace(final_output=output)

    monkeypatch.setattr(Runner, "run", no_report)
    result = asyncio.run(check_knowledge(kb_dir, context=ExecutionContext()))
    assert result.status == "completed"
    assert result.quality == ("semantic_report_missing",)
    assert result.unfinished == ("semantic_lint",)
    assert result.knowledge_report == (output or "Knowledge lint completed. No output produced.")
    assert Path(result.report_path).exists()
