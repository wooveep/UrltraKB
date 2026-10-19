"""Click deck commands use native publications and actual SDK loopback requests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from artifact_fixtures import publish_artifacts
from click.testing import CliRunner
from stream_transport import StreamTransport
from test_deck_validator import GOOD_DECK
from test_storage_deck_generation import tool_response

from ultrakb.cli import cli


def _init_kb(tmp_path: Path) -> Path:
    """Create a minimal valid KB on disk so cli's _find_kb_dir resolves.

    Includes a non-empty ``wiki/concepts/`` so the shared
    ``_preflight_skill_new`` wiki-content gate passes.
    """
    (tmp_path / ".ultrakb").mkdir()
    (tmp_path / ".ultrakb" / "config.yaml").write_text(
        "model: openai/gpt-4o\nlanguage: en\n", encoding="utf-8"
    )
    from kb_fixtures import mark_current_kb

    mark_current_kb(tmp_path)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "AGENTS.md").write_text("schema", encoding="utf-8")
    (tmp_path / "wiki" / "concepts").mkdir()
    (tmp_path / "wiki" / "concepts" / "foo.md").write_text("# foo\n", encoding="utf-8")
    from publication_read_inputs import publish_fixture

    publish_fixture(tmp_path)
    return tmp_path


def test_deck_new_help(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(cli, ["deck", "new", "--help"])
    assert result.exit_code == 0
    assert "kebab-case" in result.output.lower() or "name" in result.output.lower()


def test_deck_new_missing_skill_prints_an_error_and_preserves_the_old_deck(tmp_path, monkeypatch):
    _init_kb(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    target = tmp_path / "output/decks/demo"
    target.mkdir(parents=True)
    (target / "index.html").write_text("Original deck")
    publish_artifacts(tmp_path, target, sync=True)
    result = CliRunner().invoke(
        cli, ["deck", "new", "demo", "Intent", "--skill", "missing-native-test", "-y"]
    )
    assert result.exit_code == 1
    assert "[ERROR]" in result.output
    assert "missing-native-test" in result.output
    assert (target / "index.html").read_text() == "Original deck"


def test_deck_new_reports_the_custom_target_in_overwrite_errors(tmp_path, monkeypatch):
    _init_kb(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    skill = tmp_path / "skills/custom/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: custom\ndescription: Custom output\nod:\n"
        "  output_path_template: output/shared/index.html\n---\nDeck"
    )
    target = tmp_path / "output/shared"
    target.mkdir(parents=True)
    (target / "index.html").write_text("Original deck")
    publish_artifacts(tmp_path, target, sync=True)
    result = CliRunner().invoke(
        cli, ["deck", "new", "different-slug", "Intent", "--skill", "custom"]
    )
    assert result.exit_code == 1
    assert "output/shared/ exists" in result.output
    assert "output/decks/different-slug" not in result.output


def test_deck_new_rejects_no_kb(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .ultrakb here
    runner = CliRunner()
    with patch("ultrakb.cli._find_kb_dir", return_value=None):
        result = runner.invoke(cli, ["deck", "new", "my-deck", "An intent."])
    assert result.exit_code != 0
    assert "knowledge base" in result.output.lower()


def test_deck_new_rejects_invalid_name(tmp_path: Path, monkeypatch):
    _init_kb(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["deck", "new", "Invalid_Name", "An intent."])
    assert result.exit_code != 0
    assert "kebab" in result.output.lower() or "invalid" in result.output.lower()


def _generate_deck(root, *, critique=False, valid=True):
    from ultrakb.config import save_config

    save_config(
        root / ".ultrakb/config.yaml",
        {
            "model": "openai/gpt-4o-mini",
            "model_capacity": {"max_input_tokens": 65536},
            "model_policy": {"max_calls": 8, "retries": 0},
        },
    )
    target = "output/decks/my-deck/index.html"
    content = GOOD_DECK if valid else "<html><body>No slides.</body></html>"
    responses = [
        tool_response("write_file", {"path": target, "content": content}, "deliver"),
        {"content": "Delivered."},
    ]
    reviewed = GOOD_DECK.replace("Title", "Reviewed title")
    if critique:
        responses += [
            tool_response("write_file", {"path": target, "content": reviewed}, "review"),
            {"content": "Reviewed."},
        ]
    with StreamTransport(responses) as service:
        (root / ".env").write_text(f"LLM_API_KEY=test-key\nOPENAI_API_BASE={service.url}\n")
        args = ["--kb-dir", str(root), "deck", "new", "my-deck", "Present foo."]
        result = CliRunner().invoke(cli, args + (["--critique"] if critique else []))
    assert len(service.requests) == len(responses), result.output
    return result, (reviewed if critique else content)


def test_deck_new_happy_path(tmp_path):
    from ultrakb.application.views import published_scope
    from ultrakb.storage.artifact_store import ArtifactStore

    root = _init_kb(tmp_path)
    result, content = _generate_deck(root)
    assert result.exit_code == 0, result.output
    assert (
        ArtifactStore(root, published_scope(root).knowledge.identity).read(
            "output/decks/my-deck/index.html"
        )
        == content.encode()
    )


def test_deck_new_passes_critique_flag(tmp_path):
    root = _init_kb(tmp_path)
    result, content = _generate_deck(root, critique=True)
    assert result.exit_code == 0, result.output
    assert (root / "output/decks/my-deck/index.html").read_text() == content
    assert "Reviewed title" in content


def test_deck_new_surfaces_validation_errors(tmp_path):
    root = _init_kb(tmp_path)
    result, content = _generate_deck(root, valid=False)
    assert result.exit_code != 0, result.output
    assert "slide" in result.output.lower()
    assert (root / "output/decks/my-deck/index.html").read_text() == content


def test_deck_new_rejects_empty_wiki(tmp_path: Path, monkeypatch):
    """CLI must refuse to compile when the KB exists but the wiki is empty
    (parity with chat /deck new and CLI ultrakb skill new)."""
    (tmp_path / ".ultrakb").mkdir()
    (tmp_path / ".ultrakb" / "config.yaml").write_text(
        "model: openai/gpt-4o\nlanguage: en\n", encoding="utf-8"
    )
    from kb_fixtures import mark_current_kb

    mark_current_kb(tmp_path)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "AGENTS.md").write_text("schema", encoding="utf-8")
    # No wiki/concepts/, no wiki/summaries/ — wiki is "empty".
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    # Patch Generator so that, if preflight is missing, the test would still
    # exit 0 — making this test only pass once the wiki-content gate is wired.
    result = CliRunner().invoke(cli, ["deck", "new", "my-deck", "An intent."])

    # Should exit non-zero before ever instantiating Generator.
    assert result.exit_code != 0, result.output
    # Error message should point at the empty wiki, not at name validation.
    assert "wiki" in result.output.lower()
    assert "kebab" not in result.output.lower()
