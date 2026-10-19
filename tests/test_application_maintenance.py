"""Knowledge checks preserve committed fixes and report incomplete checks."""

import asyncio
from pathlib import Path


def test_structural_check_repairs_links_and_saves_a_report(kb_dir):
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.application.maintenance import LintOptions, check_knowledge

    page = kb_dir / "wiki/concepts/topic.md"
    page.write_text("---\ntype: Concept\ndescription: Topic\n---\n# Topic\n[[concepts/missing]]")
    context = ExecutionContext()
    from publication_read_inputs import publish_fixture

    publish_fixture(kb_dir)
    result = asyncio.run(
        check_knowledge(kb_dir, LintOptions(fix=True, semantic=False), context=context)
    )
    assert result.status == "completed"
    assert result.files_changed == result.ghosts_removed == 1
    assert "[[concepts/missing]]" not in page.read_text()
    assert "type: Concept" in page.read_text()
    assert "saved: wiki/concepts/topic.md" in result.changes

    assert "No broken links found" in result.structural_report
    assert Path(result.report_path).is_file()
    assert result.report_path in result.resources
    from native_usage_fixtures import read_requests

    assert read_requests(kb_dir) == []
    assert not result.unfinished


def test_repair_diagnostics_can_read_pages_without_replaying_journals(kb_dir):
    import json

    from ultrakb.application.repair import inspect_knowledge_base, read_diagnostic_page

    page = kb_dir / "wiki/concepts/retained.md"
    page.write_text("# Retained manual page")
    from publication_read_inputs import publish_fixture

    publish_fixture(kb_dir)
    marker = kb_dir / ".ultrakb/needs-repair.json"
    marker.write_text('{"error_type":"MissingBackup"}')
    journal = kb_dir / ".ultrakb/journal/broken.json"
    journal.parent.mkdir(exist_ok=True)
    journal.write_text(json.dumps({"status": "active", "operation": "import", "entries": []}))
    before = marker.read_bytes(), journal.read_bytes(), page.read_bytes()
    diagnostic = inspect_knowledge_base(kb_dir)
    assert diagnostic.needs_repair
    assert "concepts/retained.md" in diagnostic.pages
    assert read_diagnostic_page(kb_dir, "concepts/retained.md") == "# Retained manual page"
    assert (marker.read_bytes(), journal.read_bytes(), page.read_bytes()) == before
