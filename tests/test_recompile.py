"""Recompilation behavior through the shared operation, CLI and HTTP model."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from openkb.agent import compiler
from openkb.application.documents import import_document
from openkb.cli import cli
from openkb.schema import AGENTS_MD


@pytest.fixture(autouse=True)
def isolated_cli_history(tmp_path, monkeypatch):
    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", tmp_path / "global")


def _invoke(kb_dir, args, input_text=None):
    return CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), *args], input=input_text)


def _seed_short(kb_dir: Path):
    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps(
            {
                "h_s": {"name": "notes.md", "doc_name": "notes-h_s", "type": "md"},
            }
        )
    )
    (kb_dir / "wiki/sources/notes-h_s.md").write_text("# Notes\n\nbody\n", encoding="utf-8")


def _wiki_bytes(kb_dir):
    return {
        p.relative_to(kb_dir): p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()
    }


def test_recompile_legacy_source_requires_review_without_overwriting(kb_dir, model_service):
    _seed_short(kb_dir)
    before = _wiki_bytes(kb_dir)
    result = _invoke(kb_dir, ["recompile", "notes.md"])
    assert result.exit_code == 1, result.output
    assert "needs_acceptance" in result.output
    assert "Resume:" in result.output
    assert "intake=saved, compilation=unfinished" in result.output
    assert _wiki_bytes(kb_dir) == before
    assert len(model_service) == 3


def test_recompile_uses_saved_original_after_external_file_disappears(kb_dir, model_service):
    original = kb_dir / "notes.md"
    original.write_text("Saved original for recompilation")
    imported = import_document(kb_dir, original)
    assert imported.knowledge_compilation == "completed"
    original.unlink()
    result = _invoke(kb_dir, ["recompile", "notes.md"])
    assert result.exit_code == 0, result.output
    assert "intake=saved, compilation=completed" in result.output
    assert len(model_service) == 5


@pytest.mark.parametrize("doc_id", [None, "old-local-doc"])
def test_recompile_summary_alone_cannot_replace_missing_original(kb_dir, model_service, doc_id):
    (kb_dir / ".openkb/hashes.json").write_text(
        json.dumps(
            {
                "h_l": {
                    "name": "paper.pdf",
                    "doc_name": "paper-h_l",
                    "type": "long_pdf",
                    **({"doc_id": doc_id} if doc_id else {}),
                },
            }
        )
    )
    (kb_dir / "wiki/summaries/paper-h_l.md").write_text("# Summary without original evidence")
    before = _wiki_bytes(kb_dir)
    result = _invoke(kb_dir, ["recompile", "--all", "--yes"])
    assert result.exit_code == 1, result.output
    assert "saved_original_missing" in result.output
    assert _wiki_bytes(kb_dir) == before
    assert not model_service


def test_recompile_all_continues_after_missing_original(kb_dir, model_service):
    _seed_short(kb_dir)
    registry = kb_dir / ".openkb/hashes.json"
    registry.write_text(
        json.dumps(
            {
                "h_miss": {"name": "gone.md", "doc_name": "gone-h_miss", "type": "md"},
                **json.loads(registry.read_text()),
            }
        )
    )
    result = _invoke(kb_dir, ["recompile", "--all", "--yes"])
    assert result.exit_code == 1, result.output
    assert "saved_original_missing" in result.output
    assert "needs_acceptance" in result.output
    assert "2 document(s)" in result.output
    assert len(model_service) == 3


def test_recompile_all_decline_has_no_model_calls_or_writes(kb_dir, model_service):
    _seed_short(kb_dir)
    before = _wiki_bytes(kb_dir)
    result = _invoke(kb_dir, ["recompile", "--all"], input_text="n\n")
    assert result.exit_code == 0 and "Aborted" in result.output
    assert _wiki_bytes(kb_dir) == before
    assert not model_service


def test_recompile_dry_run_including_schema_refresh_has_no_calls_or_writes(kb_dir, model_service):
    _seed_short(kb_dir)
    before = _wiki_bytes(kb_dir)
    result = _invoke(kb_dir, ["recompile", "--all", "--dry-run", "--refresh-schema"])
    assert result.exit_code == 0 and "notes-h_s" in result.output
    assert "review and acceptance" in result.output
    assert _wiki_bytes(kb_dir) == before
    assert not model_service


@pytest.mark.parametrize(
    "args,message",
    [
        (["recompile"], "Specify a document name or pass --all"),
        (["recompile", "notes.md", "--all"], "not both"),
        (["recompile", "no-such-doc"], "No document matching"),
    ],
)
def test_recompile_invalid_selection_does_no_work(kb_dir, model_service, args, message):
    _seed_short(kb_dir)
    before = _wiki_bytes(kb_dir)
    result = _invoke(kb_dir, args)
    assert message in result.output
    assert _wiki_bytes(kb_dir) == before
    assert not model_service


def test_recompile_empty_registry_has_friendly_result(kb_dir, model_service):
    result = _invoke(kb_dir, ["recompile", "--all"])
    assert "No documents" in result.output
    assert not model_service


@pytest.mark.parametrize("mode", ["different", "same", "missing", "without_flag"])
def test_recompile_explicit_schema_refresh_preserves_backup_semantics(kb_dir, model_service, mode):
    _seed_short(kb_dir)
    agents = kb_dir / "wiki/AGENTS.md"
    if mode == "missing":
        agents.unlink(missing_ok=True)
    else:
        agents.write_text(AGENTS_MD if mode == "same" else "OLD CUSTOM SCHEMA\n", encoding="utf-8")
    args = ["recompile", "notes.md"] + ([] if mode == "without_flag" else ["--refresh-schema"])
    result = _invoke(kb_dir, args)
    assert result.exit_code == 1 and "needs_acceptance" in result.output, result.output
    backup = kb_dir / "wiki/AGENTS.md.bak"
    if mode == "different":
        assert backup.read_text(encoding="utf-8") == "OLD CUSTOM SCHEMA\n"
        assert agents.read_text(encoding="utf-8") == AGENTS_MD
    else:
        assert not backup.exists()
        if mode == "missing":
            assert not agents.exists()
        else:
            assert agents.read_text(encoding="utf-8") == (
                AGENTS_MD if mode == "same" else "OLD CUSTOM SCHEMA\n"
            )


def test_compile_long_doc_backfills_summary_frontmatter(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "summaries").mkdir(parents=True)
    (wiki / "concepts").mkdir(parents=True)
    (tmp_path / ".openkb").mkdir()
    (tmp_path / ".openkb" / "config.yaml").write_text(
        "model: gpt-4o-mini\nlanguage: en\n", encoding="utf-8"
    )
    summary_path = wiki / "summaries" / "long.md"
    summary_path.write_text(
        "---\ndoc_type: pageindex\nfull_text: sources/long.json\n---\n\n# Long\n",
        encoding="utf-8",
    )
    with (
        patch.object(compiler, "_llm_call", return_value="overview"),
        patch.object(compiler, "_compile_concepts", new=AsyncMock()),
        patch.object(compiler, "_close_async_llm_clients", new=AsyncMock()),
    ):
        asyncio.run(
            compiler.compile_long_doc(
                "long",
                summary_path,
                "doc-1",
                tmp_path,
                "gpt-4o-mini",
                doc_description="A long report.",
            )
        )
    text = summary_path.read_text(encoding="utf-8")
    assert 'type: "Summary"' in text
    assert 'description: "A long report."' in text
    # canonical order: type before description
    assert text.index("type:") < text.index("description:")
