"""Removal text algorithms retained by native knowledge maintenance.

Public source withdrawal and three-entrypoint behavior use real SQLite/CAS in
storage removal/batch-removal tests. File-registry deletion is retired.
"""

from ultrakb.agent.compiler import _remove_section_entry, _remove_source_from_frontmatter


def _native_sources(root, names):
    from ultrakb.application.views import published_scope
    from ultrakb.storage.catalog_store import CatalogStore

    identity = published_scope(root).knowledge.identity
    catalog = CatalogStore(root, identity)
    sources = []
    for name in names:
        original = root / "raw" / name
        original.write_text("Retained original: " + name)
        sources.append(catalog.admit(original, source_key=str(original)))
    return catalog, sources


def test_native_removal_keeps_exact_name_priority_and_reports_fuzzy_ambiguity(kb_dir):
    from ultrakb.application.removal import preview_removal

    catalog, sources = _native_sources(kb_dir, ("paper.md", "paper-appendix.md"))
    exact = preview_removal(kb_dir, "paper.md")
    assert exact.status == "ready" and exact.plan.file_hash == sources[0].source_id
    ambiguous = preview_removal(kb_dir, "paper")
    assert ambiguous.status == "multiple"
    assert set(ambiguous.candidates) == {source.source_id for source in sources}
    assert not any(catalog.source(source.source_id).removed for source in sources)


def test_native_cli_preview_and_declined_confirmation_preserve_all_originals(kb_dir):
    from click.testing import CliRunner

    from ultrakb.cli import cli

    catalog, (source,) = _native_sources(kb_dir, ("paper.md",))
    args = ["--kb-dir", str(kb_dir), "remove", source.source_id]
    preview = CliRunner().invoke(cli, [*args, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert "WITHDRAW" in preview.output and "RETAIN" in preview.output
    declined = CliRunner().invoke(cli, args, input="n\n")
    assert declined.exit_code == 0, declined.output
    assert not catalog.source(source.source_id).removed
    assert (kb_dir / "raw/paper.md").read_text() == "Retained original: paper.md"


def test_native_cli_removal_keeps_managed_and_external_originals(kb_dir):
    from click.testing import CliRunner

    from ultrakb.cli import cli

    catalog, (source,) = _native_sources(kb_dir, ("paper.md",))
    result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "remove", "paper.md", "--yes"])
    assert result.exit_code == 0, result.output
    assert catalog.source(source.source_id).removed
    assert catalog.read_original(source.revision_id) == b"Retained original: paper.md"
    assert (kb_dir / "raw/paper.md").read_text() == "Retained original: paper.md"


def test_remove_source_drops_only_target_and_marks_empty():
    text = "---\nsources: [summaries/a.md]\nbrief: x\n---\n\nbody\n"
    rewritten, empty = _remove_source_from_frontmatter(text, "summaries/a.md")
    assert empty is True
    assert "sources: []" in rewritten
    assert rewritten.endswith("\nbody\n")


def test_remove_source_keeps_others():
    text = "---\nsources: [summaries/a.md, summaries/b.md, summaries/c.md]\nbrief: x\n---\n\nbody\n"
    rewritten, empty = _remove_source_from_frontmatter(text, "summaries/b.md")
    assert empty is False
    assert "summaries/a.md" in rewritten
    assert "summaries/c.md" in rewritten
    assert "summaries/b.md" not in rewritten


def test_remove_source_noop_when_not_present():
    text = "---\nsources: [summaries/a.md]\n---\n\nbody\n"
    rewritten, empty = _remove_source_from_frontmatter(text, "summaries/z.md")
    assert rewritten == text
    assert empty is False


def test_remove_source_noop_without_frontmatter():
    text = "# No frontmatter\n\nbody only\n"
    rewritten, empty = _remove_source_from_frontmatter(text, "summaries/a.md")
    assert rewritten == text
    assert empty is False


def test_remove_source_noop_malformed_brackets():
    text = "---\nsources: summaries/a.md\n---\nbody\n"
    rewritten, empty = _remove_source_from_frontmatter(text, "summaries/a.md")
    assert rewritten == text
    assert empty is False


def test_remove_section_entry_strict_prefix_no_sibling_overdelete():
    lines = [
        "## Documents",
        "- [[summaries/attn-x]] (short) - The attn paper",
        "- [[summaries/survey-y]] (short) - supersedes [[summaries/attn-x]]",
        "- [[summaries/other-z]] (short) - unrelated",
        "",
        "## Concepts",
    ]
    removed = _remove_section_entry(lines, "## Documents", "[[summaries/attn-x]]")

    assert removed is True
    # Only the actual attn-x bullet is gone; the survey-y entry that
    # *mentions* attn-x in its brief survives intact.
    joined = "\n".join(lines)
    assert "- [[summaries/attn-x]]" not in joined
    assert "- [[summaries/survey-y]]" in joined
    assert "supersedes [[summaries/attn-x]]" in joined
    assert "- [[summaries/other-z]]" in joined


def test_remove_section_entry_skips_non_bullet_mentions():
    """A wikilink embedded in a paragraph (no bullet prefix) must not
    be deleted — the helper only manages canonical bullet entries.
    """
    lines = [
        "## Related Documents",
        "This concept is mostly covered by [[summaries/attn-x]] in spirit.",
        "- [[summaries/survey-y]]",
        "",
    ]
    removed = _remove_section_entry(lines, "## Related Documents", "[[summaries/attn-x]]")

    assert removed is False  # No `- [[summaries/attn-x]]` bullet in section.
    assert any("This concept is mostly covered by [[summaries/attn-x]]" in line for line in lines)
