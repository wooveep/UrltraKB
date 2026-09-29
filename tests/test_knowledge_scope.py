"""Knowledge operations share an explicit legacy range without moving content."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from openkb.application.pages import read_page, save_page
from openkb.knowledge_scope import legacy_scope


def test_explicit_legacy_scope_reads_the_existing_page(kb_dir):
    (kb_dir / "wiki/concepts/existing.md").write_text("# Existing\nRetained knowledge.")
    page = read_page(kb_dir, "concepts/existing", scope=legacy_scope(kb_dir))
    assert page.body == "# Existing\nRetained knowledge."


def test_explicit_scope_keeps_legacy_edit_and_link_behavior(kb_dir):
    (kb_dir / "wiki/concepts/existing.md").write_text("# Existing\nRetained knowledge.")
    result = save_page(
        kb_dir, "concepts/existing", "Revised [[concepts/missing]].", scope=legacy_scope(kb_dir)
    )
    assert result.page.body == "Revised missing."
    assert result.ghosts_stripped == ("concepts/missing",)


def test_explicit_legacy_scope_compiles_into_existing_wiki(kb_dir, monkeypatch):
    from openkb.agent.compiler import compile_short_doc

    source = kb_dir / "wiki/sources/notes.md"
    source.write_text("# Notes\nA source fact.")
    replies = iter(
        [
            {"description": "Notes", "content": "# Notes\n\nA source fact."},
            {"create": [], "update": [], "related": []},
        ]
    )

    def completion(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(replies))))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr("litellm.completion", completion)
    scope = legacy_scope(kb_dir)
    asyncio.run(compile_short_doc("notes", source, kb_dir, "test-model", scope=scope))
    assert (
        read_page(kb_dir, "summaries/notes", scope=scope).body.strip()
        == "# Notes\n\nA source fact."
    )


def test_scoped_chat_tools_read_the_legacy_page(kb_dir):
    from agents.tool_context import ToolContext

    from openkb.agent.query import build_chat_agent

    (kb_dir / "wiki/concepts/topic.md").write_text("The retained fact.")
    agent = build_chat_agent(kb_dir, "test-model", scope=legacy_scope(kb_dir))
    tool = next(tool for tool in agent.tools if tool.name == "read_file")
    args = '{"path":"concepts/topic.md"}'
    context = ToolContext(
        context=None, tool_name="read_file", tool_call_id="read-1", tool_arguments=args
    )
    result = asyncio.run(tool.on_invoke_tool(context, args))
    assert result == "The retained fact."


def test_scoped_maintenance_repairs_legacy_links(kb_dir):
    from openkb.application.maintenance import LintOptions, check_knowledge

    page = kb_dir / "wiki/concepts/topic.md"
    page.write_text("Retained [[concepts/missing]].")
    result = asyncio.run(
        check_knowledge(kb_dir, LintOptions(fix=True, semantic=False), scope=legacy_scope(kb_dir))
    )
    assert result.status == "completed"
    assert result.ghosts_removed == 1
    assert page.read_text() == "Retained missing."


def test_edit_rejects_a_different_knowledge_base_scope(kb_dir, tmp_path):
    page = kb_dir / "wiki/concepts/topic.md"
    page.write_text("Keep this fact.")
    with pytest.raises(ValueError, match="different knowledge base"):
        save_page(kb_dir, "concepts/topic", "Replace it.", scope=legacy_scope(tmp_path / "other"))
    assert page.read_text() == "Keep this fact."


@pytest.mark.parametrize("dry_run, status", [(True, "dry_run"), (False, "removed")])
def test_legacy_remove_accepts_a_relative_kb_root(kb_dir, monkeypatch, dry_run, status):
    from pathlib import Path

    from openkb.application.removal import run_remove_for_api
    from openkb.state import HashRegistry

    HashRegistry(kb_dir / ".openkb/hashes.json").add(
        "test-hash", {"name": "notes.pdf", "doc_name": "notes"}
    )
    (kb_dir / "wiki/summaries/notes.md").write_text("Existing summary.")
    monkeypatch.chdir(kb_dir.parent)
    result = run_remove_for_api(Path(kb_dir.name), "notes", dry_run=dry_run)
    assert result["status"] == status
