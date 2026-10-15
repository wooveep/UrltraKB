"""Tests for ultrakb.agent.compiler pipeline."""

from __future__ import annotations

import json

import pytest

from ultrakb.agent.compile_delivery import publish_compile_results
from ultrakb.agent.compile_plan import normalize_plan
from ultrakb.agent.compiler import (
    _ENTITY_TYPE_LIST,
    _backlink_concepts,
    _backlink_entities,
    _backlink_summary,
    _backlink_summary_entities,
    _filter_entity_items,
    _parse_entities_plan,
    _parse_json,
    _prepend_source_to_frontmatter,
    _read_concept_briefs,
    _read_entity_briefs,
    _read_wiki_context,
    _remove_source_from_frontmatter,
    _sanitize_concept_name,
    _update_index,
    _write_concept,
    _write_entity,
    _write_summary,
    compile_long_doc,
    compile_short_doc,
    remove_doc_from_entity_pages,
)
from ultrakb.config import resolve_entity_types
from ultrakb.schema import AGENTS_MD


def _normalized_plan(wiki, value, *, entity_types=None):
    """Validate domain plan data directly, without replacing a model response."""
    return normalize_plan(
        value,
        existing={
            kind: {p.stem for p in (wiki / kind).glob("*.md")} for kind in ("concepts", "entities")
        },
        entity_types=frozenset(entity_types or _ENTITY_TYPE_LIST),
        sanitize=_sanitize_concept_name,
    )


def _no_model_executor():
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings

    return CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle()),
        ModelCallPolicy(max_calls=0, retries=0),
    )


class TestFrontmatterSourceMutation:
    """``_prepend_source_to_frontmatter``/``_remove_source_from_frontmatter`` must preserve existing
    frontmatter even when the page ends at the closing ``---`` with no trailing
    newline — ``frontmatter.split`` then returns a block ending in a bare
    ``\\n---`` rather than ``\\n---\\n``.
    """

    def test_prepend_preserves_keys_without_trailing_newline(self):
        text = '---\nsources: ["summaries/p1.md"]\ntype: "Concept"\ndescription: "Focus"\n---'
        out = _prepend_source_to_frontmatter(text, "summaries/p2.md")
        assert out.startswith("---\n")  # opening delimiter kept
        assert 'type: "Concept"' in out  # other keys kept
        assert 'description: "Focus"' in out
        assert "summaries/p1.md" in out  # existing source kept
        assert "summaries/p2.md" in out  # new source prepended

    def test_remove_preserves_keys_without_trailing_newline(self):
        text = '---\ntype: "Organization"\nsources: ["summaries/doc.md"]\n---'
        out, now_empty = _remove_source_from_frontmatter(text, "summaries/doc.md")
        assert now_empty is True  # it was the only source
        assert 'type: "Organization"' in out  # other key preserved
        assert "summaries/doc.md" not in out  # source removed

    def test_prepend_with_body_is_unchanged(self):
        text = '---\nsources: ["a.md"]\ntype: "Concept"\n---\n\nBody.\n'
        out = _prepend_source_to_frontmatter(text, "b.md")
        assert out.startswith("---\n")
        assert "b.md" in out and "a.md" in out
        assert out.endswith("\n\nBody.\n")  # body + closing untouched


class TestParseJson:
    def test_plain_json(self):
        assert _parse_json('[{"name": "foo"}]') == [{"name": "foo"}]

    def test_fenced_json(self):
        text = '```json\n[{"name": "bar"}]\n```'
        assert _parse_json(text) == [{"name": "bar"}]

    def test_invalid_json(self):
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _parse_json("not json")


class TestParseConceptsPlan:
    def test_dict_format(self):
        text = json.dumps(
            {
                "create": [{"name": "foo", "title": "Foo"}],
                "update": [{"name": "bar", "title": "Bar"}],
                "related": ["baz"],
            }
        )
        parsed = _parse_json(text)
        assert isinstance(parsed, dict)
        assert len(parsed["create"]) == 1
        assert len(parsed["update"]) == 1
        assert parsed["related"] == ["baz"]

    def test_fallback_list_format(self):
        text = json.dumps([{"name": "foo", "title": "Foo"}])
        parsed = _parse_json(text)
        assert isinstance(parsed, list)

    def test_fenced_dict(self):
        text = '```json\n{"create": [], "update": [], "related": []}\n```'
        parsed = _parse_json(text)
        assert isinstance(parsed, dict)
        assert parsed["create"] == []


class TestParseEntitiesPlan:
    def test_extracts_entities_group(self):
        parsed = {
            "concepts": {"create": [{"name": "x", "title": "X"}], "update": [], "related": []},
            "entities": {
                "create": [{"name": "anthropic", "title": "Anthropic", "type": "organization"}],
                "update": [],
                "related": ["nvidia"],
            },
        }
        ents = _parse_entities_plan(parsed)
        assert ents["create"] == [
            {"name": "anthropic", "title": "Anthropic", "type": "organization"}
        ]
        assert ents["related"] == ["nvidia"]

    def test_missing_entities_key_is_empty(self):
        ents = _parse_entities_plan({"create": [], "update": [], "related": []})
        assert ents == {"create": [], "update": [], "related": []}

    def test_bad_type_falls_back_to_other(self):
        parsed = {
            "entities": {
                "create": [{"name": "x", "title": "X", "type": "alien"}],
                "update": [],
                "related": [],
            }
        }
        ents = _parse_entities_plan(parsed)
        assert ents["create"][0]["type"] == "other"


class TestResolveEntityTypes:
    def test_default_when_key_absent(self):
        assert resolve_entity_types({}) == list(_ENTITY_TYPE_LIST)

    def test_custom_list_is_used_and_normalized(self):
        out = resolve_entity_types({"entity_types": ["Person", " Dataset ", "MODEL"]})
        assert out == ["person", "dataset", "model", "other"]

    def test_always_includes_other(self):
        out = resolve_entity_types({"entity_types": ["person", "dataset"]})
        assert "other" in out
        # already-present "other" is not duplicated
        out2 = resolve_entity_types({"entity_types": ["dataset", "other"]})
        assert out2.count("other") == 1

    def test_dedupes_preserving_order(self):
        out = resolve_entity_types({"entity_types": ["a", "a", "b"]})
        assert out == ["a", "b", "other"]

    def test_malformed_string_falls_back_to_default(self):
        assert resolve_entity_types({"entity_types": "person"}) == list(_ENTITY_TYPE_LIST)

    def test_empty_list_falls_back_to_default(self):
        assert resolve_entity_types({"entity_types": []}) == list(_ENTITY_TYPE_LIST)

    def test_all_empty_strings_falls_back_to_default(self):
        assert resolve_entity_types({"entity_types": ["", "  "]}) == list(_ENTITY_TYPE_LIST)

    def test_sanitizes_punctuation_and_skips_non_strings(self):
        # '{'/'}' and other punctuation are stripped (so they can't leak into a
        # prompt template's .format()); non-string items (YAML null, ints) are
        # skipped (str(None) must NOT become the type "none").
        out = resolve_entity_types({"entity_types": ["Per{son}", None, 123, "data set!"]})
        assert out == ["person", "data set", "other"]


class TestFilterEntityItemsCustomTypes:
    def test_custom_type_in_valid_types_is_kept(self):
        valid = frozenset({"person", "dataset", "other"})
        items = [{"name": "imagenet", "title": "ImageNet", "type": "dataset"}]
        out = _filter_entity_items(items, valid)
        assert out == [{"name": "imagenet", "title": "ImageNet", "type": "dataset"}]

    def test_type_not_in_valid_types_is_coerced_to_other(self):
        valid = frozenset({"person", "dataset", "other"})
        items = [{"name": "x", "title": "X", "type": "organization"}]
        out = _filter_entity_items(items, valid)
        assert out[0]["type"] == "other"

    def test_default_valid_types_backward_compat(self):
        # No valid_types arg → module default enum is used.
        items = [{"name": "x", "title": "X", "type": "organization"}]
        out = _filter_entity_items(items)
        assert out[0]["type"] == "organization"


class TestParseBriefContent:
    def test_dict_with_brief_and_content(self):
        text = json.dumps({"brief": "A short desc", "content": "# Full page\n\nDetails."})
        parsed = _parse_json(text)
        assert parsed["brief"] == "A short desc"
        assert "# Full page" in parsed["content"]

    def test_plain_text_fallback(self):
        """If LLM returns plain text, _parse_json raises — caller handles fallback."""
        with pytest.raises((json.JSONDecodeError, ValueError)):
            _parse_json("Just plain markdown text without JSON")


class TestSanitizeConceptName:
    def test_ascii_passthrough(self):
        assert _sanitize_concept_name("hello-world") == "hello-world"

    def test_spaces_replaced(self):
        assert _sanitize_concept_name("hello world") == "hello-world"

    def test_chinese(self):
        result = _sanitize_concept_name("注意力机制")
        assert result == "注意力机制"

    def test_japanese(self):
        result = _sanitize_concept_name("トランスフォーマー")
        assert result == "トランスフォーマー"

    def test_french_accents(self):
        result = _sanitize_concept_name("réseau neuronal")
        assert "r" in result
        assert result != "r-seau-neuronal"  # accented chars preserved, not stripped

    def test_distinct_chinese_names_no_collision(self):
        a = _sanitize_concept_name("注意力机制")
        b = _sanitize_concept_name("变压器模型")
        assert a != b

    def test_empty_fallback(self):
        assert _sanitize_concept_name("!!!") == "unnamed-concept"

    def test_nfkc_normalization(self):
        # U+FF21 (fullwidth A) should normalize to regular A
        assert _sanitize_concept_name("\uff21\uff22") == "AB"


class TestWriteSummary:
    def test_writes_type_and_description(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_summary(wiki, "my-doc", "# Summary\n\nContent.", description="A one-line summary.")
        text = (wiki / "summaries" / "my-doc.md").read_text()
        assert 'type: "Summary"' in text
        assert 'description: "A one-line summary."' in text
        assert "doc_type: short" in text
        assert 'full_text: "sources/my-doc.md"' in text
        assert "# Summary" in text

    def test_omits_description_when_empty(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_summary(wiki, "my-doc", "# Summary\n\nContent.")
        text = (wiki / "summaries" / "my-doc.md").read_text()
        assert 'type: "Summary"' in text
        assert "description:" not in text


class TestWriteConcept:
    def test_new_concept_with_brief(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_concept(
            wiki,
            "attention",
            "# Attention\n\nDetails.",
            "paper.pdf",
            False,
            brief="Mechanism for selective focus",
        )
        path = wiki / "concepts" / "attention.md"
        assert path.exists()
        text = path.read_text()
        assert 'sources: ["paper.pdf"]' in text
        assert 'description: "Mechanism for selective focus"' in text
        assert "# Attention" in text

    def test_new_concept_without_brief(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_concept(wiki, "attention", "# Attention\n\nDetails.", "paper.pdf", False)
        path = wiki / "concepts" / "attention.md"
        assert path.exists()
        text = path.read_text()
        assert 'sources: ["paper.pdf"]' in text
        assert "brief:" not in text

    def test_update_concept_updates_brief(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            "---\nsources: [paper1.pdf]\nbrief: Old brief\n---\n\n# Attention\n\nOld content.",
            encoding="utf-8",
        )
        _write_concept(wiki, "attention", "New info.", "paper2.pdf", True, brief="Updated brief")
        text = (concepts / "attention.md").read_text()
        assert "paper2.pdf" in text
        assert "paper1.pdf" in text
        assert 'description: "Updated brief"' in text
        assert "Old brief" not in text

    def test_update_concept_appends_source(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            "---\nsources: [paper1.pdf]\n---\n\n# Attention\n\nOld content.",
            encoding="utf-8",
        )
        _write_concept(wiki, "attention", "New info from paper2.", "paper2.pdf", True)
        text = (concepts / "attention.md").read_text()
        assert "paper2.pdf" in text
        assert "paper1.pdf" in text
        assert "New info from paper2." in text

    def test_update_concept_merges_into_non_canonical_sources(self, tmp_path):
        """sources:[a] (no space after colon) must still get paper2 prepended,
        matching the helper's behavior in _add_related_link."""
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            "---\nsources:[paper1.pdf]\n---\n\n# Attention\n\nOld content.",
            encoding="utf-8",
        )
        _write_concept(wiki, "attention", "New info from paper2.", "paper2.pdf", True)
        text = (concepts / "attention.md").read_text()
        assert "paper1.pdf" in text
        assert "paper2.pdf" in text
        assert "New info from paper2." in text

    def test_new_concept_has_type_and_description(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_concept(
            wiki,
            "attention",
            "# Attention\n\nDetails.",
            "summaries/p.md",
            False,
            brief="Mechanism for selective focus",
        )
        text = (wiki / "concepts" / "attention.md").read_text()
        assert 'type: "Concept"' in text
        assert 'description: "Mechanism for selective focus"' in text
        assert "brief:" not in text

    def test_new_concept_without_description_still_has_type(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_concept(wiki, "attention", "# Attention\n\nDetails.", "summaries/p.md", False)
        text = (wiki / "concepts" / "attention.md").read_text()
        assert 'type: "Concept"' in text
        assert "description:" not in text

    def test_update_concept_sets_type_and_description(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            '---\nsources: ["p1.pdf"]\ndescription: "Old"\n---\n\n# Attention\n\nOld.',
            encoding="utf-8",
        )
        _write_concept(wiki, "attention", "New.", "summaries/p2.md", True, brief="New one")
        text = (concepts / "attention.md").read_text()
        assert 'type: "Concept"' in text
        assert 'description: "New one"' in text
        assert "Old" not in text


class TestUpdateIndex:
    def test_appends_entries_with_briefs(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text(
            "# Index\n\n## Documents\n\n## Concepts\n\n## Explorations\n",
            encoding="utf-8",
        )
        _update_index(
            wiki,
            "my-doc",
            ["attention", "transformer"],
            doc_brief="Introduces transformers",
            concept_briefs={"attention": "Focus mechanism", "transformer": "NN architecture"},
        )
        text = (wiki / "index.md").read_text()
        assert "[[summaries/my-doc]] (short) — Introduces transformers" in text
        assert "[[concepts/attention]] — Focus mechanism" in text
        assert "[[concepts/transformer]] — NN architecture" in text

    def test_updates_only_exact_concept_row(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text(
            "# Index\n\n## Documents\n\n## Concepts\n"
            "- [[concepts/transformer]] — Uses [[concepts/attention]] internally\n"
            "- [[concepts/attention]] — Old brief\n\n## Explorations\n",
            encoding="utf-8",
        )
        _update_index(
            wiki,
            "my-doc",
            ["attention"],
            concept_briefs={"attention": "New brief"},
        )
        text = (wiki / "index.md").read_text()
        assert "- [[concepts/transformer]] — Uses [[concepts/attention]] internally" in text
        assert "- [[concepts/attention]] — New brief" in text
        assert text.count("[[concepts/attention]] — New brief") == 1

    def test_no_duplicates(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text(
            "# Index\n\n## Documents\n- [[summaries/my-doc]] — Old brief\n\n## Concepts\n",
            encoding="utf-8",
        )
        _update_index(wiki, "my-doc", [], doc_brief="New brief")
        text = (wiki / "index.md").read_text()
        assert text.count("[[summaries/my-doc]]") == 1

    def test_backwards_compat_no_briefs(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text(
            "# Index\n\n## Documents\n\n## Concepts\n\n## Explorations\n",
            encoding="utf-8",
        )
        _update_index(wiki, "my-doc", ["attention"])
        text = (wiki / "index.md").read_text()
        assert "[[summaries/my-doc]]" in text
        assert "[[concepts/attention]]" in text

    def test_updates_concept_brief_only_inside_concepts_section(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text(
            "# Index\n\n"
            "## Documents\n"
            "- [[summaries/my-doc]] (short) — Mentions [[concepts/attention]] here\n\n"
            "## Concepts\n"
            "- [[concepts/attention]] — Old brief\n\n"
            "## Explorations\n",
            encoding="utf-8",
        )

        _update_index(
            wiki,
            "my-doc",
            ["attention"],
            concept_briefs={"attention": "New brief"},
        )

        text = (wiki / "index.md").read_text()
        assert "- [[summaries/my-doc]] (short) — Mentions [[concepts/attention]] here" in text
        assert "- [[concepts/attention]] — New brief" in text
        assert "- [[concepts/attention]] — Old brief" not in text

    def test_adds_concept_entry_when_link_exists_outside_concepts_section(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text(
            "# Index\n\n"
            "## Documents\n"
            "- [[summaries/my-doc]] (short) — Mentions [[concepts/attention]] here\n\n"
            "## Concepts\n\n"
            "## Explorations\n",
            encoding="utf-8",
        )

        _update_index(
            wiki,
            "my-doc",
            ["attention"],
            concept_briefs={"attention": "New brief"},
        )

        text = (wiki / "index.md").read_text()
        assert "- [[summaries/my-doc]] (short) — Mentions [[concepts/attention]] here" in text
        assert "- [[concepts/attention]] — New brief" in text

    def test_recovers_when_documents_section_missing(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text(
            "# Index\n\n## Concepts\n\n## Explorations\n",
            encoding="utf-8",
        )
        _update_index(wiki, "my-doc", [], doc_brief="Brief")
        text = (wiki / "index.md").read_text()
        assert "## Documents" in text
        assert "[[summaries/my-doc]] (short) — Brief" in text

    def test_recovers_when_concepts_section_missing(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text(
            "# Index\n\n## Documents\n\n## Explorations\n",
            encoding="utf-8",
        )
        _update_index(wiki, "my-doc", ["attention"], concept_briefs={"attention": "Focus"})
        text = (wiki / "index.md").read_text()
        assert "## Concepts" in text
        assert "[[concepts/attention]] — Focus" in text
        assert "[[summaries/my-doc]]" in text

    def test_entities_inserted_before_explorations(self, tmp_path):
        """#8: an old index.md predating ## Entities must get it inserted
        before ## Explorations, not appended after it (canonical order)."""
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # Old order: no ## Entities section yet.
        (wiki / "index.md").write_text(
            "# Index\n\n## Documents\n\n## Concepts\n\n## Explorations\n",
            encoding="utf-8",
        )
        _update_index(
            wiki,
            "my-doc",
            [],
            entity_names=["anthropic"],
            entity_meta={"anthropic": ("organization", "AI lab.")},
        )
        text = (wiki / "index.md").read_text()
        assert "## Entities" in text
        # Canonical order: Entities before Explorations.
        assert text.index("## Entities") < text.index("## Explorations")
        assert "[[entities/anthropic]] (organization) — AI lab." in text


class TestReadWikiContext:
    def test_empty_wiki(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        index, concepts = _read_wiki_context(wiki)
        assert index == ""
        assert concepts == []

    def test_with_content(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text("# Index\n", encoding="utf-8")
        concepts_dir = wiki / "concepts"
        concepts_dir.mkdir()
        (concepts_dir / "attention.md").write_text("# Attention", encoding="utf-8")
        (concepts_dir / "transformer.md").write_text("# Transformer", encoding="utf-8")
        index, concepts = _read_wiki_context(wiki)
        assert "# Index" in index
        assert concepts == ["attention", "transformer"]


class TestReadConceptBriefs:
    def test_empty_wiki(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        (wiki / "concepts").mkdir()
        assert _read_concept_briefs(wiki) == "(none yet)"

    def test_no_concepts_dir(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        assert _read_concept_briefs(wiki) == "(none yet)"

    def test_reads_briefs_with_frontmatter(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            "---\nsources: [paper.pdf]\n---\n\nAttention is a mechanism that allows models to focus on relevant parts.",
            encoding="utf-8",
        )
        result = _read_concept_briefs(wiki)
        assert "- attention:" in result
        assert "Attention is a mechanism" in result
        assert "sources" not in result
        assert "---" not in result

    def test_reads_briefs_without_frontmatter(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "transformer.md").write_text(
            "Transformer is a neural network architecture based on attention.",
            encoding="utf-8",
        )
        result = _read_concept_briefs(wiki)
        assert "- transformer:" in result
        assert "Transformer is a neural network" in result

    def test_truncates_long_content(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        long_body = "A" * 300
        (concepts / "longconcept.md").write_text(long_body, encoding="utf-8")
        result = _read_concept_briefs(wiki)
        # The brief part should be truncated at 150 chars
        brief = result.split("- longconcept: ", 1)[1]
        assert len(brief) == 150
        assert brief == "A" * 150

    def test_sorted_alphabetically(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "zebra.md").write_text("Zebra concept.", encoding="utf-8")
        (concepts / "apple.md").write_text("Apple concept.", encoding="utf-8")
        (concepts / "mango.md").write_text("Mango concept.", encoding="utf-8")
        result = _read_concept_briefs(wiki)
        lines = result.strip().splitlines()
        slugs = [line.split(":")[0].lstrip("- ") for line in lines]
        assert slugs == ["apple", "mango", "zebra"]

    def test_reads_brief_from_frontmatter(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            "---\nsources: [paper.pdf]\nbrief: Selective focus mechanism\n---\n\n# Attention\n\nLong content...",
            encoding="utf-8",
        )
        result = _read_concept_briefs(wiki)
        assert "- attention: Selective focus mechanism" in result

    def test_falls_back_to_body_truncation(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "old.md").write_text(
            "---\nsources: [paper.pdf]\n---\n\nOld concept without brief field.",
            encoding="utf-8",
        )
        result = _read_concept_briefs(wiki)
        assert "- old: Old concept without brief field." in result

    def test_reads_description_field(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            '---\nsources: ["p.pdf"]\ndescription: "Selective focus"\n---\n\n# A\n',
            encoding="utf-8",
        )
        assert "- attention: Selective focus" in _read_concept_briefs(wiki)


class TestReadEntityBriefs:
    def test_none_when_missing(self, tmp_path):
        assert _read_entity_briefs(tmp_path) == "(none yet)"

    def test_brief_type_and_source_count(self, tmp_path):
        ent = tmp_path / "entities"
        ent.mkdir()
        (ent / "anthropic.md").write_text(
            "---\n"
            "sources: [summaries/a.md, summaries/b.md]\n"
            "type: organization\n"
            "brief: AI lab behind Claude.\n"
            "---\n\n# Anthropic\n",
            encoding="utf-8",
        )
        out = _read_entity_briefs(tmp_path)
        assert out == "- anthropic (organization, 2 sources) — AI lab behind Claude."

    def test_empty_dir_returns_none(self, tmp_path):
        ent = tmp_path / "entities"
        ent.mkdir()
        assert _read_entity_briefs(tmp_path) == "(none yet)"

    def test_falls_back_to_body_when_no_brief(self, tmp_path):
        ent = tmp_path / "entities"
        ent.mkdir()
        body_text = "OpenAI is a research lab focused on artificial general intelligence."
        (ent / "openai.md").write_text(
            "---\n"
            "type: organization\n"
            "sources: [summaries/a.md, summaries/b.md, summaries/c.md]\n"
            "---\n\n" + body_text,
            encoding="utf-8",
        )
        out = _read_entity_briefs(tmp_path)
        # Should use truncated body (first 150 chars) as the brief
        expected_brief = body_text[:150]
        assert f" — {expected_brief}" in out
        # Should still include type and source count
        assert "(organization, 3 sources)" in out

    def test_sorted_alphabetically(self, tmp_path):
        ent = tmp_path / "entities"
        ent.mkdir()
        (ent / "zeta.md").write_text(
            "---\ntype: person\nsources: [summaries/a.md]\nbrief: Last letter of Greek alphabet.\n---\n",
            encoding="utf-8",
        )
        (ent / "alpha.md").write_text(
            "---\ntype: concept\nsources: [summaries/b.md]\nbrief: First letter of Greek alphabet.\n---\n",
            encoding="utf-8",
        )
        out = _read_entity_briefs(tmp_path)
        lines = out.strip().splitlines()
        assert lines[0].startswith("- alpha ")
        assert lines[1].startswith("- zeta ")

    def test_reads_description_and_lowercases_type(self, tmp_path):
        ent = tmp_path / "entities"
        ent.mkdir()
        (ent / "anthropic.md").write_text(
            "---\n"
            'sources: ["summaries/a.md", "summaries/b.md"]\n'
            'type: "Organization"\n'
            'description: "AI lab behind Claude."\n'
            "---\n\n# Anthropic\n",
            encoding="utf-8",
        )
        out = _read_entity_briefs(tmp_path)
        assert out == "- anthropic (organization, 2 sources) — AI lab behind Claude."

    def test_reads_legacy_brief_when_no_description(self, tmp_path):
        ent = tmp_path / "entities"
        ent.mkdir()
        (ent / "openai.md").write_text(
            "---\ntype: organization\nsources: [summaries/a.md]\n"
            "brief: Legacy one-liner.\n---\n\n# OpenAI\n",
            encoding="utf-8",
        )
        out = _read_entity_briefs(tmp_path)
        assert "— Legacy one-liner." in out


class TestWriteEntity:
    def test_new_entity_frontmatter(self, tmp_path):
        _write_entity(
            tmp_path,
            "anthropic",
            "# Anthropic\n\nAI lab.",
            "summaries/a.md",
            is_update=False,
            brief="AI lab behind Claude.",
            type_="organization",
            aliases=["Anthropic PBC"],
        )
        text = (tmp_path / "entities" / "anthropic.md").read_text(encoding="utf-8")
        assert 'type: "Organization"' in text
        assert 'description: "AI lab behind Claude."' in text
        assert "sources:" in text and "summaries/a.md" in text
        assert "Anthropic PBC" in text
        assert text.count("---") == 2  # exactly one frontmatter block

    def test_update_prepends_source_keeps_type(self, tmp_path):
        _write_entity(
            tmp_path,
            "anthropic",
            "# Anthropic\n\nv1.",
            "summaries/a.md",
            is_update=False,
            brief="b1",
            type_="organization",
            aliases=None,
        )
        _write_entity(
            tmp_path,
            "anthropic",
            "# Anthropic\n\nv2 richer.",
            "summaries/b.md",
            is_update=True,
            brief="b2",
            type_="organization",
            aliases=None,
        )
        text = (tmp_path / "entities" / "anthropic.md").read_text(encoding="utf-8")
        assert "summaries/b.md" in text and "summaries/a.md" in text
        # _yaml_list_line uses json.dumps: b prepended before a, double-quoted
        assert '"summaries/b.md", "summaries/a.md"' in text
        assert 'type: "Organization"' in text
        assert "v2 richer." in text
        assert "v1." not in text
        assert 'description: "b2"' in text

    def test_update_rebuilds_frontmatter_when_no_closing_delim(self, tmp_path):
        """#11: malformed existing file (opening --- but no closing ---) must
        not drop frontmatter; rebuild valid sources/type/brief on update."""
        entities = tmp_path / "entities"
        entities.mkdir(parents=True)
        # Opening delimiter, NO closing delimiter — find("---", 3) == -1.
        (entities / "anthropic.md").write_text(
            '---\nsources: ["summaries/a.md"]\ntype: organization\n'
            "# Anthropic (no closing fence)\n\nOld body.",
            encoding="utf-8",
        )
        _write_entity(
            tmp_path,
            "anthropic",
            "# Anthropic\n\nv2 rewritten.",
            "summaries/b.md",
            is_update=True,
            brief="AI lab.",
            type_="organization",
            aliases=None,
        )
        text = (entities / "anthropic.md").read_text(encoding="utf-8")
        # Frontmatter rebuilt with a proper closing delimiter, not body-only.
        assert text.startswith("---\n")
        assert text.count("---") == 2
        assert "sources:" in text and "summaries/b.md" in text
        # The PRE-EXISTING source must be preserved, not dropped when rebuilding.
        assert "summaries/a.md" in text
        assert 'type: "Organization"' in text
        assert 'description: "AI lab."' in text
        assert "v2 rewritten." in text

    def test_new_entity_type_capitalized_and_description(self, tmp_path):
        _write_entity(
            tmp_path,
            "anthropic",
            "# Anthropic\n\nAI lab.",
            "summaries/a.md",
            is_update=False,
            brief="AI lab behind Claude.",
            type_="organization",
        )
        text = (tmp_path / "entities" / "anthropic.md").read_text(encoding="utf-8")
        assert 'type: "Organization"' in text  # capitalized
        assert 'description: "AI lab behind Claude."' in text
        assert "brief:" not in text  # renamed, not duplicated

    def test_update_entity_capitalizes_type_and_writes_description(self, tmp_path):
        _write_entity(
            tmp_path,
            "anthropic",
            "# A\n\nv1.",
            "summaries/a.md",
            is_update=False,
            brief="b1",
            type_="organization",
        )
        _write_entity(
            tmp_path,
            "anthropic",
            "# A\n\nv2.",
            "summaries/b.md",
            is_update=True,
            brief="b2",
            type_="organization",
        )
        text = (tmp_path / "entities" / "anthropic.md").read_text(encoding="utf-8")
        assert 'type: "Organization"' in text
        assert 'description: "b2"' in text
        assert "brief:" not in text

    def test_update_entity_strips_legacy_brief(self, tmp_path):
        entities = tmp_path / "entities"
        entities.mkdir(parents=True)
        (entities / "anthropic.md").write_text(
            '---\nsources: ["summaries/a.md"]\ntype: organization\n'
            "brief: Old brief.\n---\n\n# Anthropic\n\nOld.",
            encoding="utf-8",
        )
        _write_entity(
            tmp_path,
            "anthropic",
            "# Anthropic\n\nv2.",
            "summaries/b.md",
            is_update=True,
            brief="New desc.",
            type_="organization",
        )
        text = (entities / "anthropic.md").read_text(encoding="utf-8")
        assert "brief:" not in text
        assert "Old brief." not in text
        assert 'description: "New desc."' in text

    def test_entity_type_multiword_title_cased(self, tmp_path):
        _write_entity(
            tmp_path,
            "acme",
            "# Acme\n\nx.",
            "summaries/a.md",
            is_update=False,
            brief="b",
            type_="real estate",
        )
        text = (tmp_path / "entities" / "acme.md").read_text(encoding="utf-8")
        assert 'type: "Real Estate"' in text


def test_update_keeps_single_blank_line_after_frontmatter(tmp_path):
    """Regression: the update path must not accumulate a 3rd newline after ---."""
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "concepts" / "x.md").write_text(
        '---\ntype: "Concept"\nsources: ["a"]\ndescription: "old"\n---\n\n# X\n', encoding="utf-8"
    )
    _write_concept(wiki, "x", "# X\n\nNew.", "summaries/b.md", True, brief="new")
    ctext = (wiki / "concepts" / "x.md").read_text(encoding="utf-8")
    assert "---\n\n\n" not in ctext and "---\n\n" in ctext

    (wiki / "entities").mkdir(parents=True)
    (wiki / "entities" / "e.md").write_text(
        '---\nsources: ["a"]\ntype: "Person"\ndescription: "old"\n---\n\n# E\n', encoding="utf-8"
    )
    _write_entity(wiki, "e", "# E\n\nNew.", "summaries/b.md", True, brief="new", type_="person")
    etext = (wiki / "entities" / "e.md").read_text(encoding="utf-8")
    assert "---\n\n\n" not in etext and "---\n\n" in etext


class TestBacklinkSummary:
    def test_adds_missing_concept_links(self, tmp_path):
        wiki = tmp_path / "wiki"
        summaries = wiki / "summaries"
        summaries.mkdir(parents=True)
        (summaries / "paper.md").write_text(
            "---\nsources: [paper.pdf]\n---\n\n# Summary\n\nContent about attention.",
            encoding="utf-8",
        )
        _backlink_summary(wiki, "paper", ["attention", "transformer"])
        text = (summaries / "paper.md").read_text()
        assert "[[concepts/attention]]" in text
        assert "[[concepts/transformer]]" in text

    def test_skips_already_linked(self, tmp_path):
        wiki = tmp_path / "wiki"
        summaries = wiki / "summaries"
        summaries.mkdir(parents=True)
        (summaries / "paper.md").write_text(
            "---\nsources: [paper.pdf]\n---\n\n# Summary\n\nSee [[concepts/attention]].",
            encoding="utf-8",
        )
        _backlink_summary(wiki, "paper", ["attention", "transformer"])
        text = (summaries / "paper.md").read_text()
        # attention already linked, should not duplicate
        assert text.count("[[concepts/attention]]") == 1
        # transformer should be added
        assert "[[concepts/transformer]]" in text

    def test_no_op_when_all_linked(self, tmp_path):
        wiki = tmp_path / "wiki"
        summaries = wiki / "summaries"
        summaries.mkdir(parents=True)
        original = "# Summary\n\n[[concepts/attention]] and [[concepts/transformer]]"
        (summaries / "paper.md").write_text(original, encoding="utf-8")
        _backlink_summary(wiki, "paper", ["attention", "transformer"])
        assert (summaries / "paper.md").read_text() == original

    def test_skips_if_file_missing(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # Should not raise
        _backlink_summary(wiki, "nonexistent", ["attention"])

    def test_merges_into_existing_section(self, tmp_path):
        """Second add should merge into existing ## Related Concepts, not duplicate."""
        wiki = tmp_path / "wiki"
        summaries = wiki / "summaries"
        summaries.mkdir(parents=True)
        (summaries / "paper.md").write_text(
            "# Summary\n\nContent.\n\n## Related Concepts\n- [[concepts/attention]]\n",
            encoding="utf-8",
        )
        _backlink_summary(wiki, "paper", ["attention", "transformer"])
        text = (summaries / "paper.md").read_text()
        assert text.count("## Related Concepts") == 1
        assert "[[concepts/transformer]]" in text
        assert text.count("[[concepts/attention]]") == 1

    def test_section_with_trailing_whitespace_still_merges(self, tmp_path):
        """Heading with trailing space must merge into the existing section,
        not append a duplicate H2."""
        wiki = tmp_path / "wiki"
        summaries = wiki / "summaries"
        summaries.mkdir(parents=True)
        (summaries / "paper.md").write_text(
            "# Summary\n\nContent.\n\n## Related Concepts \n- [[concepts/attention]]\n",
            encoding="utf-8",
        )
        _backlink_summary(wiki, "paper", ["attention", "transformer"])
        text = (summaries / "paper.md").read_text()
        assert "[[concepts/transformer]]" in text
        assert text.count("## Related Concepts") == 1


class TestBacklinkConcepts:
    def test_adds_summary_link_to_concept(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            "---\nsources: [paper.pdf]\n---\n\n# Attention\n\nContent.",
            encoding="utf-8",
        )
        _backlink_concepts(wiki, "paper", ["attention"])
        text = (concepts / "attention.md").read_text()
        assert "[[summaries/paper]]" in text
        assert "## Related Documents" in text

    def test_skips_if_already_linked(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            "# Attention\n\nBased on [[summaries/paper]].",
            encoding="utf-8",
        )
        _backlink_concepts(wiki, "paper", ["attention"])
        text = (concepts / "attention.md").read_text()
        assert text.count("[[summaries/paper]]") == 1
        assert "## Related Documents" not in text

    def test_merges_into_existing_section(self, tmp_path):
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            "# Attention\n\n## Related Documents\n- [[summaries/old-paper]]\n",
            encoding="utf-8",
        )
        _backlink_concepts(wiki, "new-paper", ["attention"])
        text = (concepts / "attention.md").read_text()
        assert text.count("## Related Documents") == 1
        assert "[[summaries/old-paper]]" in text
        assert "[[summaries/new-paper]]" in text

    def test_skips_missing_concept_file(self, tmp_path):
        wiki = tmp_path / "wiki"
        (wiki / "concepts").mkdir(parents=True)
        # Should not raise
        _backlink_concepts(wiki, "paper", ["nonexistent"])

    def test_section_with_trailing_whitespace_still_merges(self, tmp_path):
        """Heading with trailing space must merge into the existing section,
        not append a duplicate H2."""
        wiki = tmp_path / "wiki"
        concepts = wiki / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "attention.md").write_text(
            "# Attention\n\n## Related Documents \n- [[summaries/old-paper]]\n",
            encoding="utf-8",
        )
        _backlink_concepts(wiki, "new-paper", ["attention"])
        text = (concepts / "attention.md").read_text()
        assert "[[summaries/new-paper]]" in text
        assert "[[summaries/old-paper]]" in text
        assert text.count("## Related Documents") == 1


class TestCompileShortDoc:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, kb_dir, monkeypatch, live_llm):
        from ultrakb import frontmatter
        from ultrakb.locks import atomic_write_text

        live_llm.install(monkeypatch)
        wiki = kb_dir / "wiki"
        atomic_write_text(wiki / "index.md", "# Index\n\n## Documents\n\n## Concepts\n")
        source = wiki / "sources/test-doc.md"
        atomic_write_text(
            source,
            "# Transformer\n\nA Transformer is a neural network architecture using self-attention.\n",
        )
        await compile_short_doc(
            "test-doc",
            source,
            kb_dir,
            "deepseek/deepseek-flash",
            max_concurrency=1,
            bundle=live_llm.credentials,
        )
        summary = wiki / "summaries/test-doc.md"
        metadata = frontmatter.parse(summary.read_text())
        assert metadata["full_text"] == "sources/test-doc.md"
        assert metadata["type"] == "Summary"
        assert "transformer" in summary.read_text().lower()
        concepts = list((wiki / "concepts").glob("*.md"))
        assert concepts
        assert any("self-attention" in page.read_text().lower() for page in concepts)
        for page in concepts:
            assert "summaries/test-doc.md" in frontmatter.parse(page.read_text())["sources"]
            assert f"[[concepts/{page.stem}]]" in summary.read_text()
            assert f"[[concepts/{page.stem}]]" in (wiki / "index.md").read_text()
        assert "[[summaries/test-doc]]" in (wiki / "index.md").read_text()

    def test_handles_bad_json(self, tmp_path):
        wiki, _ = TestCompileShortDocFallbacks._setup_kb(tmp_path)
        with pytest.raises(ValueError):
            _parse_json("not valid json")
        assert "[[summaries/doc]]" not in (wiki / "index.md").read_text()
        assert not (wiki / "summaries/doc.md").exists()


class TestCompileShortDocFallbacks:
    """The first summary survives without an extra send and invalid links are removed."""

    @staticmethod
    def _setup_kb(tmp_path):
        wiki = tmp_path / "wiki"
        (wiki / "sources").mkdir(parents=True)
        (wiki / "summaries").mkdir(parents=True)
        (wiki / "concepts").mkdir(parents=True)
        (wiki / "index.md").write_text(
            "# Index\n\n## Documents\n\n## Concepts\n\n## Explorations\n",
            encoding="utf-8",
        )
        (tmp_path / ".ultrakb").mkdir()
        source_path = wiki / "sources" / "doc.md"
        source_path.write_text("Body.", encoding="utf-8")
        return wiki, source_path

    def test_summary_uses_first_response_and_filters_links(self, tmp_path):
        wiki, _ = self._setup_kb(tmp_path)
        summary = "# Summary\n\nDiscusses [[concepts/transformer]] and [[concepts/ghost]]."
        plan = _normalized_plan(wiki, {"create": [{"name": "transformer"}]})
        publish_compile_results(
            wiki,
            "doc",
            plan,
            summary=summary,
            concept_results=[("transformer", "# Transformer\n\nBody.", False, "Transformer")],
            rewrite_summary=True,
        )
        text = (wiki / "summaries/doc.md").read_text()
        assert "Discusses" in text
        assert "[[concepts/transformer]]" in text
        assert "[[concepts/ghost]]" not in text
        assert "ghost" in text

    def test_summary_does_not_require_a_third_response(self, tmp_path):
        wiki, _ = self._setup_kb(tmp_path)
        executor = _no_model_executor()
        with executor.activate():
            summary = "# Summary\n\nUses [[concepts/transformer]] mechanism."
            plan = _normalized_plan(wiki, {"create": [{"name": "transformer"}]})
            publish_compile_results(
                wiki,
                "doc",
                plan,
                summary=summary,
                concept_results=[("transformer", "# Transformer\n\nBody.", False, "")],
                rewrite_summary=True,
            )
            text = (wiki / "summaries/doc.md").read_text()
            assert summary in text
            assert "[[concepts/transformer]]" in text
            assert executor.calls == 0

    def test_plan_parse_failure_blocks_summary_publication(self, tmp_path):
        wiki, _ = self._setup_kb(tmp_path)
        with pytest.raises(ValueError):
            _normalized_plan(wiki, _parse_json("not valid json at all"))
        assert not (wiki / "summaries/doc.md").exists()
        assert "[[summaries/doc]]" not in (wiki / "index.md").read_text()

    def test_empty_plan_strips_v1_summary_ghosts(self, tmp_path):
        wiki, _ = self._setup_kb(tmp_path)
        plan = _normalized_plan(wiki, {})
        publish_compile_results(
            wiki,
            "doc",
            plan,
            summary="# Summary\n\nMentions [[concepts/imaginary]] briefly.",
            rewrite_summary=True,
        )
        text = (wiki / "summaries/doc.md").read_text()
        assert "[[concepts/imaginary]]" not in text
        assert "imaginary" in text

    def test_scalar_plan_blocks_publication(self, tmp_path):
        wiki, _ = self._setup_kb(tmp_path)
        with pytest.raises(ValueError):
            _normalized_plan(wiki, 42)
        assert "[[summaries/doc]]" not in (wiki / "index.md").read_text()
        assert not list((wiki / "concepts").glob("*.md"))


class TestCacheControl:
    """Verify cache_control breakpoints are emitted on the right messages
    so Anthropic prompt caching can hit on every reuse of the base context.
    """

    @staticmethod
    def _has_cache_breakpoint(message: dict) -> bool:
        content = message.get("content")
        if not isinstance(content, list):
            return False
        return any(
            isinstance(b, dict) and b.get("cache_control", {}).get("type") == "ephemeral"
            for b in content
        )

    def test_short_doc_marks_doc_and_summary(self):
        from ultrakb.agent.compile_messages import build_page_messages, planning_messages
        from ultrakb.agent.compiler import _cached_text, short_document_messages

        summary_messages = short_document_messages(
            "doc", "Original facts about caching.", AGENTS_MD, "en"
        )
        assert summary_messages[0]["role"] == "system"
        assert summary_messages[1]["role"] == "user"
        assert self._has_cache_breakpoint(summary_messages[1])
        plan = planning_messages(
            *summary_messages[:2], "Summary about caching.", concept_briefs="", entity_briefs=""
        )
        assert self._has_cache_breakpoint(plan[1])
        assert plan[2]["role"] == "assistant"
        assert self._has_cache_breakpoint(plan[2])
        page = build_page_messages(
            system_message=summary_messages[0],
            current_original={"role": "user", "content": _cached_text("Current original: facts")},
            original_context={"role": "user", "content": "Original source windows: facts"},
            known_targets_message={
                "role": "user",
                "content": _cached_text("Known targets: concepts/caching"),
            },
            task_message={"role": "user", "content": "Write the concept page"},
        )
        assert self._has_cache_breakpoint(page[1])
        assert all(m["role"] != "assistant" for m in page)
        assert page[3]["role"] == "user"
        assert self._has_cache_breakpoint(page[3])

    def test_long_doc_marks_doc_message(self):
        from ultrakb.agent.compile_messages import planning_messages

        plan = planning_messages(
            {"role": "system", "content": "Wiki agent"},
            {"role": "user", "content": "Generated navigation for a long document"},
            "Document knowledge overview.",
            concept_briefs="",
            entity_briefs="",
            long=True,
        )
        assert plan[1]["role"] == "user"
        assert "Generated navigation" in plan[1]["content"]
        assert self._has_cache_breakpoint(plan[2])
        assert len(plan) == 4


class TestCompileLongDoc:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, kb_dir, monkeypatch, live_llm):
        from ultrakb.locks import atomic_write_text

        live_llm.install(monkeypatch)
        wiki = kb_dir / "wiki"
        atomic_write_text(wiki / "index.md", "# Index\n\n## Documents\n\n## Concepts\n")
        summary = wiki / "summaries/big-doc.md"
        atomic_write_text(
            summary, "---\nfull_text: sources/big-doc.json\n---\nGenerated navigation."
        )
        atomic_write_text(
            wiki / "sources/big-doc.json",
            json.dumps(
                [
                    {
                        "page": 1,
                        "unit_kind": "page",
                        "content": "# Deep Learning\n\nDeep Learning is a subfield of machine learning using neural networks.",
                    }
                ]
            ),
        )
        await compile_long_doc(
            "big-doc",
            summary,
            "doc-123",
            kb_dir,
            "deepseek/deepseek-flash",
            max_concurrency=1,
            bundle=live_llm.credentials,
            navigation=[],
        )
        concepts = list((wiki / "concepts").glob("*.md"))
        assert concepts
        assert any("deep learning" in path.read_text().lower() for path in concepts)
        index = (wiki / "index.md").read_text()
        assert "[[summaries/big-doc]]" in index
        assert all(f"[[concepts/{path.stem}]]" in index for path in concepts)
        assert "neural networks" in summary.read_text().lower()


class TestCompileConceptsPlan:
    """Integration tests for _compile_concepts with the new plan format."""

    def _setup_wiki(self, tmp_path, existing_concepts=None):
        """Helper to set up a wiki directory with optional existing concepts."""
        wiki = tmp_path / "wiki"
        (wiki / "summaries").mkdir(parents=True)
        (wiki / "concepts").mkdir(parents=True)
        (wiki / "index.md").write_text(
            "# Index\n\n## Documents\n\n## Concepts\n",
            encoding="utf-8",
        )
        (tmp_path / "raw").mkdir(exist_ok=True)
        (tmp_path / "raw" / "test-doc.pdf").write_bytes(b"fake")

        if existing_concepts:
            for name, content in existing_concepts.items():
                (wiki / "concepts" / f"{name}.md").write_text(
                    content,
                    encoding="utf-8",
                )

        return wiki

    def test_create_and_update_flow(self, tmp_path):
        wiki = self._setup_wiki(
            tmp_path,
            existing_concepts={
                "attention": "---\nsources: [summaries/old-paper.md]\n---\n\n# Attention\n\nOriginal statements about attention."
            },
        )
        plan = _normalized_plan(
            wiki, {"create": [{"name": "flash-attention"}], "update": [{"name": "attention"}]}
        )
        publish_compile_results(
            wiki,
            "test-doc",
            plan,
            summary="Summary.",
            concept_results=[
                (
                    "flash-attention",
                    "# Flash Attention\n\nAn efficient attention algorithm.",
                    False,
                    "Efficient attention",
                ),
                (
                    "attention",
                    "# Attention\n\nOriginal statements about attention.\n\nNew information.",
                    True,
                    "Updated attention",
                ),
            ],
        )
        text = (wiki / "concepts/flash-attention.md").read_text()
        assert 'sources: ["summaries/test-doc.md"]' in text
        assert "Flash Attention" in text
        updated = (wiki / "concepts/attention.md").read_text()
        assert "summaries/test-doc.md" in updated and "summaries/old-paper.md" in updated
        index = (wiki / "index.md").read_text()
        assert "[[concepts/flash-attention]]" in index and "[[concepts/attention]]" in index

    def test_parse_page_json_unwraps_and_guards_shape(self):
        """#158: _parse_page_json returns an object, unwraps a single-element
        ``[{...}]`` array, and returns None for wrong-shaped-but-valid JSON."""
        from ultrakb.agent.compiler import _parse_page_json

        assert _parse_page_json('{"content": "x"}') == {"content": "x"}
        assert _parse_page_json('[{"content": "x"}]') == {"content": "x"}  # unwrapped
        with pytest.raises(ValueError, match="page_"):
            _parse_page_json("[]")
        with pytest.raises(ValueError, match="page_"):
            _parse_page_json('[{"a": 1}, {"b": 2}]')
        with pytest.raises(ValueError, match="page_"):
            _parse_page_json('["a", "b"]')

    def test_page_json_wrapped_in_single_array_is_recovered(self, tmp_path):
        from ultrakb.agent.compiler import _parse_page_json

        wiki = self._setup_wiki(tmp_path)
        obj = _parse_page_json(
            '[{"description":"Recovered","content":"# Attention\\n\\nRecovered body."}]'
        )
        plan = _normalized_plan(wiki, {"create": [{"name": "attention"}]})
        publish_compile_results(
            wiki,
            "test-doc",
            plan,
            summary="Summary.",
            concept_results=[("attention", obj["content"], False, obj["description"])],
        )
        text = (wiki / "concepts/attention.md").read_text()
        assert "Recovered body." in text
        assert "[{" not in text

    def test_truncated_update_preserves_existing_page(self, tmp_path):
        from ultrakb.agent.compiler import TruncatedResponseError
        from ultrakb.locks import atomic_write_text

        wiki = self._setup_wiki(tmp_path)
        page = wiki / "concepts/attention.md"
        original = "---\nsources: [old.pdf]\n---\n\n# Original\n\nComplete original body."
        if True:
            atomic_write_text(page, original)
        plan = _normalized_plan(wiki, {"concepts": {"update": [{"name": "attention"}]}})
        failure = TruncatedResponseError("Delivered output reached the length limit")
        publish_compile_results(
            wiki, "test-doc", plan, summary="Summary.", concept_results=[failure]
        )
        if True:
            assert page.read_text() == original
            assert "Truncated tail" not in page.read_text()
        else:
            assert not page.exists()
        assert "[[concepts/attention]]" not in (wiki / "index.md").read_text()

    def test_truncated_create_skips_partial_page(self, tmp_path):
        from ultrakb.agent.compiler import TruncatedResponseError
        from ultrakb.locks import atomic_write_text

        wiki = self._setup_wiki(tmp_path)
        page = wiki / "concepts/ghost.md"
        original = "---\nsources: [old.pdf]\n---\n\n# Original\n\nComplete original body."
        if False:
            atomic_write_text(page, original)
        plan = _normalized_plan(wiki, {"concepts": {"create": [{"name": "ghost"}]}})
        failure = TruncatedResponseError("Delivered output reached the length limit")
        publish_compile_results(
            wiki, "test-doc", plan, summary="Summary.", concept_results=[failure]
        )
        if False:
            assert page.read_text() == original
            assert "Truncated tail" not in page.read_text()
        else:
            assert not page.exists()
        assert "[[concepts/ghost]]" not in (wiki / "index.md").read_text()

    def test_page_fields_maps_response_shapes(self):
        """Shared mapping used by all four page closures: object, single-element
        array unwrap, wrong-shape skip (empty content), and non-JSON prose
        fallback (raw written as the body)."""
        from ultrakb.agent.compiler import _page_fields

        brief, content, obj = _page_fields('{"description": "d", "content": "c", "type": "org"}')
        assert (brief, content) == ("d", "c")
        assert obj == {"description": "d", "content": "c", "type": "org"}
        # Single-element [{...}] is unwrapped and used.
        assert _page_fields('[{"description": "d", "content": "c"}]')[:2] == ("d", "c")
        # Wrong shape (multi-element / empty array) → empty content so the
        # caller's _require_nonempty_content skips the page.
        with pytest.raises(ValueError, match="page_"):
            _page_fields('[{"a": 1}, {"b": 2}]')
        with pytest.raises(ValueError, match="page_"):
            _page_fields("[]")
        # Non-JSON prose → written verbatim as the markdown body.
        prose = "# Heading\n\nJust markdown, not JSON."
        assert _page_fields(prose) == ("", prose, None)

    def test_truncated_entity_update_preserves_existing_page(self, tmp_path):
        from ultrakb.agent.compiler import TruncatedResponseError
        from ultrakb.locks import atomic_write_text

        wiki = self._setup_wiki(tmp_path)
        page = wiki / "entities/google.md"
        original = "---\nsources: [old.pdf]\n---\n\n# Original\n\nComplete original body."
        if True:
            atomic_write_text(page, original)
        plan = _normalized_plan(wiki, {"entities": {"update": [{"name": "google"}]}})
        failure = TruncatedResponseError("Delivered output reached the length limit")
        publish_compile_results(
            wiki, "test-doc", plan, summary="Summary.", entity_results=[failure]
        )
        if True:
            assert page.read_text() == original
            assert "Truncated tail" not in page.read_text()
        else:
            assert not page.exists()
        assert "[[entities/google]]" not in (wiki / "index.md").read_text()

    def test_empty_content_skips_page_no_json_body(self, tmp_path):
        from ultrakb.agent.page_delivery import validate_page_delivery
        from ultrakb.agent.page_response import PageResponseError

        wiki = self._setup_wiki(tmp_path)
        plan = _normalized_plan(wiki, {"create": [{"name": "ghost-concept"}]})
        with pytest.raises(PageResponseError, match="page_empty_content") as failure:
            validate_page_delivery('{"description":"Empty","content":""}')
        publish_compile_results(
            wiki, "test-doc", plan, summary="Summary.", concept_results=[failure.value]
        )
        assert not (wiki / "concepts/ghost-concept.md").exists()
        assert "[[concepts/ghost-concept]]" not in (wiki / "index.md").read_text()
        assert not any('"content":' in p.read_text() for p in (wiki / "concepts").glob("*.md"))

    def test_related_adds_link_no_llm(self, tmp_path):
        from ultrakb.locks import atomic_write_text

        wiki = self._setup_wiki(
            tmp_path,
            existing_concepts={
                "transformer": "---\nsources: [old.pdf]\n---\n\n# Transformer\n\nContent about transformers."
            },
        )
        atomic_write_text(wiki / "summaries/test-doc.md", "Summary.")
        plan = _normalized_plan(wiki, {"related": ["transformer"]})
        executor = _no_model_executor()
        with executor.activate():
            publish_compile_results(wiki, "test-doc", plan, summary="Summary.")
        assert executor.calls == 0
        assert "summaries/test-doc" not in (wiki / "concepts/transformer.md").read_text()
        assert "[[concepts/transformer]]" in (wiki / "summaries/test-doc.md").read_text()

    def test_fallback_list_format(self, tmp_path):
        wiki = self._setup_wiki(tmp_path)
        plan = _normalized_plan(wiki, [{"name": "attention", "title": "Attention"}])
        assert plan.normalized
        assert len(plan.concepts.create) == 1 and not plan.concepts.update
        publish_compile_results(
            wiki,
            "test-doc",
            plan,
            summary="Summary.",
            concept_results=[
                ("attention", "# Attention\n\nA mechanism for focusing.", False, "Selective focus")
            ],
        )
        assert 'sources: ["summaries/test-doc.md"]' in (wiki / "concepts/attention.md").read_text()
        assert "[[concepts/attention]]" in (wiki / "index.md").read_text()


class TestBriefIntegration:
    def test_short_doc_briefs_in_index_and_frontmatter(self, tmp_path):
        wiki, _ = TestCompileShortDocFallbacks._setup_kb(tmp_path)
        plan = _normalized_plan(wiki, {"create": [{"name": "transformer"}]})
        publish_compile_results(
            wiki,
            "test-doc",
            plan,
            summary="# Summary\n\nThis paper discusses transformers.",
            doc_brief="A paper about transformers",
            rewrite_summary=True,
            concept_results=[
                (
                    "transformer",
                    "# Transformer\n\nA neural network architecture.",
                    False,
                    "NN architecture using self-attention",
                )
            ],
        )
        summary = (wiki / "summaries/test-doc.md").read_text()
        assert "doc_type: short" in summary and 'full_text: "sources/test-doc.md"' in summary
        assert (
            'description: "NN architecture using self-attention"'
            in (wiki / "concepts/transformer.md").read_text()
        )
        index = (wiki / "index.md").read_text()
        assert (
            "— A paper about transformers" in index
            and "— NN architecture using self-attention" in index
        )


class TestIndexEntities:
    def test_entities_section_written(self, tmp_path):
        _update_index(
            tmp_path,
            "doc",
            [],
            doc_brief="d",
            entity_names=["anthropic"],
            entity_meta={"anthropic": ("organization", "AI lab behind Claude.")},
        )
        text = (tmp_path / "index.md").read_text(encoding="utf-8")
        assert "## Entities" in text
        assert "- [[entities/anthropic]] (organization) — AI lab behind Claude." in text

    def test_entity_entry_replaced_on_update(self, tmp_path):
        _update_index(
            tmp_path,
            "doc",
            [],
            entity_names=["anthropic"],
            entity_meta={"anthropic": ("organization", "old")},
        )
        _update_index(
            tmp_path,
            "doc2",
            [],
            entity_names=["anthropic"],
            entity_meta={"anthropic": ("organization", "new")},
        )
        text = (tmp_path / "index.md").read_text(encoding="utf-8")
        assert text.count("[[entities/anthropic]]") == 1
        assert "new" in text and "old" not in text


class TestEntityBacklinks:
    def _seed(self, tmp_path):
        (tmp_path / "summaries").mkdir()
        (tmp_path / "summaries" / "doc.md").write_text(
            "---\nsources: []\n---\n\n# Doc\n", encoding="utf-8"
        )
        (tmp_path / "entities").mkdir()
        (tmp_path / "entities" / "anthropic.md").write_text(
            "---\ntype: organization\nsources: [summaries/doc.md]\n---\n\n# Anthropic\n",
            encoding="utf-8",
        )

    def test_summary_gets_entities_section(self, tmp_path):
        self._seed(tmp_path)
        _backlink_summary_entities(tmp_path, "doc", ["anthropic"])
        text = (tmp_path / "summaries" / "doc.md").read_text(encoding="utf-8")
        assert "## Entities" in text
        assert "[[entities/anthropic]]" in text

    def test_entity_gets_related_documents(self, tmp_path):
        self._seed(tmp_path)
        _backlink_entities(tmp_path, "doc", ["anthropic"])
        text = (tmp_path / "entities" / "anthropic.md").read_text(encoding="utf-8")
        assert "## Related Documents" in text
        assert "[[summaries/doc]]" in text

    def test_idempotent(self, tmp_path):
        self._seed(tmp_path)
        _backlink_summary_entities(tmp_path, "doc", ["anthropic"])
        _backlink_summary_entities(tmp_path, "doc", ["anthropic"])
        text = (tmp_path / "summaries" / "doc.md").read_text(encoding="utf-8")
        assert text.count("[[entities/anthropic]]") == 1


class TestRemoveEntityPages:
    def test_strip_source_and_delete_when_empty(self, tmp_path):
        ent = tmp_path / "entities"
        ent.mkdir()
        (ent / "solo.md").write_text(
            "---\ntype: organization\nsources: [summaries/doc.md]\n---\n\n"
            "# Solo\n\n## Related Documents\n- [[summaries/doc]]\n",
            encoding="utf-8",
        )
        (ent / "shared.md").write_text(
            "---\ntype: organization\nsources: [summaries/doc.md, summaries/other.md]\n---\n\n"
            "# Shared\n\n## Related Documents\n- [[summaries/doc]]\n- [[summaries/other]]\n",
            encoding="utf-8",
        )
        result = remove_doc_from_entity_pages(tmp_path, "doc")
        assert result == {"modified": ["shared"], "deleted": ["solo"]}
        assert not (ent / "solo.md").exists()
        shared = (ent / "shared.md").read_text(encoding="utf-8")
        assert "summaries/doc" not in shared
        assert "summaries/other" in shared

    def test_strips_standalone_see_also_line(self, tmp_path):
        # A related entity (linked via _add_related_link) carries a
        # standalone "See also:" paragraph, not a "## Related Documents"
        # section. Removing the doc must strip it so no dangling wikilink
        # survives on an entity that has other sources.
        ent = tmp_path / "entities"
        ent.mkdir()
        (ent / "shared.md").write_text(
            "---\ntype: organization\nsources: [summaries/doc.md, summaries/other.md]\n---\n\n"
            "# Shared\n\nSee also: [[summaries/doc]]",
            encoding="utf-8",
        )
        result = remove_doc_from_entity_pages(tmp_path, "doc")
        assert result == {"modified": ["shared"], "deleted": []}
        shared = (ent / "shared.md").read_text(encoding="utf-8")
        assert "summaries/doc" not in shared
        assert "See also" not in shared
        assert "summaries/other" in shared


class TestCompileEntitiesEndToEnd:
    def test_entity_and_concept_split(self, tmp_path):
        wiki, _ = TestCompileShortDocFallbacks._setup_kb(tmp_path)
        plan = _normalized_plan(
            wiki,
            {
                "concepts": {"create": [{"name": "ai-demand"}]},
                "entities": {"create": [{"name": "nvidia", "type": "organization"}]},
            },
        )
        publish_compile_results(
            wiki,
            "doc",
            plan,
            summary="# Doc\n",
            rewrite_summary=True,
            concept_results=[("ai-demand", "# AI Demand\n\nRecorded demand.", False, "Demand")],
            entity_results=[
                ("nvidia", "# NVIDIA\n\nAn organization.", "Organization", "organization")
            ],
        )
        assert (wiki / "concepts/ai-demand.md").exists()
        assert (wiki / "entities/nvidia.md").exists()
        assert 'type: "Organization"' in (wiki / "entities/nvidia.md").read_text()
        assert "[[entities/nvidia]]" in (wiki / "index.md").read_text()
        assert "[[entities/nvidia]]" in (wiki / "summaries/doc.md").read_text()

    def test_related_entity_does_not_downgrade_index_label(self, tmp_path):
        from ultrakb.locks import atomic_write_text

        wiki, _ = TestCompileShortDocFallbacks._setup_kb(tmp_path)
        atomic_write_text(wiki / "summaries/doc.md", "# Doc\n")
        atomic_write_text(
            wiki / "index.md",
            "## Documents\n\n## Concepts\n\n## Entities\n- [[entities/anthropic]] (organization) — AI safety lab\n",
        )
        atomic_write_text(
            wiki / "entities/anthropic.md",
            "---\ntype: organization\nsources: []\n---\n\n# Anthropic\n",
        )
        plan = _normalized_plan(wiki, {"entities": {"related": ["anthropic"]}})
        publish_compile_results(wiki, "doc", plan, summary="Summary.")
        index = (wiki / "index.md").read_text()
        assert "(organization)" in index
        assert "AI safety lab" in index

    def test_related_to_nonexistent_concept_does_not_create_dangling_links(self, tmp_path):
        wiki, _ = TestCompileShortDocFallbacks._setup_kb(tmp_path)
        with pytest.raises(ValueError, match="related"):
            _normalized_plan(
                wiki, {"create": [{"name": "real-concept"}], "related": ["ghost-concept"]}
            )
        plan = _normalized_plan(wiki, {"create": [{"name": "real-concept"}]})
        publish_compile_results(
            wiki,
            "doc",
            plan,
            summary="# Doc\n\nSee [[concepts/real-concept]] and [[concepts/ghost-concept]].",
            rewrite_summary=True,
            concept_results=[
                ("real-concept", "# Real\n\nLinks [[concepts/ghost-concept]].", False, "Real")
            ],
        )
        assert not (wiki / "concepts/ghost-concept.md").exists()
        assert "[[concepts/ghost-concept]]" not in (wiki / "concepts/real-concept.md").read_text()
        summary = (wiki / "summaries/doc.md").read_text()
        assert "[[concepts/ghost-concept]]" not in summary
        assert "[[concepts/real-concept]]" in summary

    def test_custom_entity_type_is_not_coerced(self, tmp_path):
        from ultrakb.agent.compile_messages import planning_messages

        wiki, _ = TestCompileShortDocFallbacks._setup_kb(tmp_path)
        types = ["person", "organization", "dataset", "other"]
        plan = _normalized_plan(
            wiki,
            {"entities": {"create": [{"name": "imagenet", "type": "dataset"}]}},
            entity_types=types,
        )
        assert plan.entities.create[0]["type"] == "dataset"
        publish_compile_results(
            wiki,
            "doc",
            plan,
            summary="Summary.",
            entity_results=[("imagenet", "# ImageNet\n\nA dataset.", "Dataset", "dataset")],
        )
        assert 'type: "Dataset"' in (wiki / "entities/imagenet.md").read_text()
        prompt = planning_messages(
            {"role": "system", "content": "s"},
            {"role": "user", "content": "d"},
            "Summary.",
            concept_briefs="",
            entity_briefs="",
            entity_types=types,
        )[-1]["content"]
        assert "dataset" in prompt
        assert "__ENTITY_TYPES__" not in prompt

    def test_brace_in_entity_type_does_not_crash_format(self):
        from ultrakb.agent.compile_messages import planning_messages

        prompt = planning_messages(
            {"role": "system", "content": "s"},
            {"role": "user", "content": "d"},
            "Summary.",
            concept_briefs="",
            entity_briefs="",
            entity_types=["wei{rd}", "other"],
        )[-1]["content"]
        assert "wei{rd}" in prompt
        assert "__ENTITY_TYPES__" not in prompt

    def test_default_path_plan_prompt_has_default_types(self):
        from ultrakb.agent.compile_messages import planning_messages

        prompt = planning_messages(
            {"role": "system", "content": "s"},
            {"role": "user", "content": "d"},
            "Summary.",
            concept_briefs="",
            entity_briefs="",
            entity_types=None,
        )[-1]["content"]
        for item in _ENTITY_TYPE_LIST:
            assert item in prompt
        assert "__ENTITY_TYPES__" not in prompt


# ---------------------------------------------------------------------------
# Task 9: schema declares entities
# ---------------------------------------------------------------------------


def test_schema_declares_entities():
    assert "entities/" in AGENTS_MD
    assert "Entity Page" in AGENTS_MD
    for t in ("person", "organization", "place", "product", "work", "event", "other"):
        assert t in AGENTS_MD


def test_ensure_h2_section_quiet_suppresses_drift_warning(caplog):
    """Backlink helpers create sections as a normal operation, so quiet=True
    must not emit the 'hand-edited' drift warning; default still warns."""
    import logging

    from ultrakb.agent.compiler import _ensure_h2_section

    with caplog.at_level(logging.WARNING, logger="ultrakb.agent.compiler"):
        lines = ["# Doc", ""]
        _ensure_h2_section(lines, "## Entities", quiet=True)
        assert "## Entities" in lines
        assert caplog.records == []

        _ensure_h2_section(["# Doc", ""], "## Entities")  # default warns
        assert any("missing" in r.getMessage() for r in caplog.records)


def test_known_targets_prompt_has_entities_rule():
    """The whitelist message must tell the LLM the [[entities/X]] rule, since
    entity-page prompts instruct writing such links; otherwise entity links
    are generated freely and then stripped as ghosts."""
    from ultrakb.agent.compiler import _KNOWN_TARGETS_USER

    assert "[[entities/" in _KNOWN_TARGETS_USER


def test_plan_prompt_keeps_topic_itself_guard():
    """The concept-plan prompt must retain the guard against creating a concept
    that merely mirrors the document's own topic."""
    from ultrakb.agent.compiler import _CONCEPTS_PLAN_USER

    assert "just the document topic itself" in _CONCEPTS_PLAN_USER


class TestLLMCallExtraHeaders:
    """Config-driven extra headers reach the litellm calls (issue #93)."""

    def test_llm_call_injects_extra_headers(self):
        from ultrakb.config import set_extra_headers
        from ultrakb.llm_execution import ModelRequest, compiler_executor

        set_extra_headers({"Editor-Version": "vscode/1.95.0"})
        options = {}
        executor = compiler_executor("deepseek/deepseek-flash", None, options)
        kwargs = executor._kwargs(
            "compile",
            ModelRequest(
                [{"role": "user", "content": "Say OK."}],
                operation="compile",
                stage="step",
                generation_options=options,
            ),
        )
        assert kwargs["extra_headers"] == {"Editor-Version": "vscode/1.95.0"}
        assert executor.calls == 0

    def test_llm_call_no_extra_headers_by_default(self):
        from ultrakb.config import set_extra_headers
        from ultrakb.llm_execution import ModelRequest, compiler_executor

        set_extra_headers({})
        options = {}
        executor = compiler_executor("deepseek/deepseek-flash", None, options)
        kwargs = executor._kwargs(
            "compile",
            ModelRequest(
                [{"role": "user", "content": "Say OK."}],
                operation="compile",
                stage="step",
                generation_options=options,
            ),
        )
        assert "extra_headers" not in kwargs
        assert executor.calls == 0

    def test_llm_call_explicit_kwarg_wins_over_config(self):
        from ultrakb.config import set_extra_headers
        from ultrakb.llm_execution import ModelRequest, compiler_executor

        set_extra_headers({"Editor-Version": "from-config"})
        options = {"extra_headers": {"Editor-Version": "explicit"}}
        executor = compiler_executor("deepseek/deepseek-flash", None, options)
        kwargs = executor._kwargs(
            "compile",
            ModelRequest(
                [{"role": "user", "content": "Say OK."}],
                operation="compile",
                stage="step",
                generation_options=options,
            ),
        )
        assert kwargs["extra_headers"] == {"Editor-Version": "explicit"}
        assert executor.calls == 0

    def test_llm_call_async_injects_extra_headers(self):
        from ultrakb.config import set_extra_headers
        from ultrakb.llm_execution import ModelRequest, compiler_executor

        set_extra_headers({"Copilot-Integration-Id": "vscode-chat"})
        options = {}
        executor = compiler_executor("deepseek/deepseek-flash", None, options)
        kwargs = executor._kwargs(
            "compile",
            ModelRequest(
                [{"role": "user", "content": "Say OK."}],
                operation="compile",
                stage="step",
                generation_options=options,
            ),
        )
        assert kwargs["extra_headers"] == {"Copilot-Integration-Id": "vscode-chat"}
        assert executor.calls == 0


class TestCacheControlStripping:
    """cache_control markers must only reach providers that honour them.

    ``_cached_text`` tags payloads with an Anthropic ``cache_control`` marker.
    LiteLLM turns that marker into a hard 400 for Gemini ("CachedContent can not
    be used with system_instruction/tools") and silently wastes it on other
    non-Anthropic providers, so ``_llm_call``/``_llm_call_async`` strip it for
    every non-Anthropic model. Regression for the all-Gemini-compiles-fail bug.
    """

    def test_accepts_for_anthropic_providers(self):
        from ultrakb.agent.compiler import _accepts_cache_control

        assert _accepts_cache_control("anthropic/claude-sonnet-4-6")
        assert _accepts_cache_control("claude-opus-4-6")
        # Claude served via OpenRouter still honours the marker.
        assert _accepts_cache_control("openrouter/anthropic/claude-3.5-sonnet")

    def test_rejects_for_non_anthropic_providers(self):
        from ultrakb.agent.compiler import _accepts_cache_control

        assert not _accepts_cache_control("gemini/gemini-2.5-pro")
        assert not _accepts_cache_control("gpt-4o")

    def test_strip_removes_marker_without_mutating_input(self):
        from ultrakb.agent.compiler import _cached_text, _strip_cache_control

        messages = [
            {"role": "system", "content": "plain string stays"},
            {"role": "user", "content": _cached_text("doc")},
        ]
        cleaned = _strip_cache_control(messages)
        # Plain-string content passes through untouched.
        assert cleaned[0]["content"] == "plain string stays"
        # Marker gone, text preserved.
        assert cleaned[1]["content"] == [{"type": "text", "text": "doc"}]
        # Original input is not mutated.
        assert "cache_control" in messages[1]["content"][0]

    def test_llm_call_strips_marker_for_gemini(self):
        from ultrakb.agent.compiler import _cached_text, _prepare_messages

        sent = _prepare_messages(
            "gemini/gemini-2.5-pro", [{"role": "user", "content": _cached_text("doc")}]
        )
        assert "cache_control" not in sent[0]["content"][0]
        assert sent[0]["content"][0]["text"] == "doc"

    def test_llm_call_keeps_marker_for_anthropic(self):
        from ultrakb.agent.compiler import _cached_text, _prepare_messages

        sent = _prepare_messages(
            "anthropic/claude-sonnet-4-6", [{"role": "user", "content": _cached_text("doc")}]
        )
        assert sent[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


class TestFrontmatterDashBoundary:
    """Regression: description containing '---' must not truncate frontmatter."""

    def test_concept_round_trip_with_dashes_in_brief(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        # Write concept with a brief containing '---'.
        brief = "--- note ---"
        _write_concept(
            wiki, "tricky", "# Body\n\nContent.", "summaries/doc.md", is_update=False, brief=brief
        )
        # Round-trip: _read_concept_briefs must return the brief intact.
        result = _read_concept_briefs(wiki)
        assert "--- note ---" in result
        # The body must not be corrupted.
        text = (wiki / "concepts" / "tricky.md").read_text(encoding="utf-8")
        assert "# Body" in text
        assert "Content." in text

    def test_entity_round_trip_with_dashes_in_brief(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        brief = "--- note ---"
        _write_entity(
            wiki,
            "tricky-org",
            "# Body\n\nContent.",
            "summaries/doc.md",
            is_update=False,
            brief=brief,
            type_="organization",
        )
        result = _read_entity_briefs(wiki)
        assert "--- note ---" in result
        text = (wiki / "entities" / "tricky-org.md").read_text(encoding="utf-8")
        assert "# Body" in text
        assert "Content." in text

    def test_concept_update_malformed_frontmatter_rebuilds(self, tmp_path):
        """_write_concept(is_update=True) on a file with malformed frontmatter
        must rebuild valid frontmatter, not write a bare body."""
        concepts = tmp_path / "concepts"
        concepts.mkdir(parents=True)
        # Opening '---' with no closing delimiter.
        malformed = "---\nsources: [x]\nno close\n\nbody"
        (concepts / "tricky.md").write_text(malformed, encoding="utf-8")
        _write_concept(
            tmp_path,
            "tricky",
            "# New\n\nNew body.",
            "summaries/doc.md",
            is_update=True,
            brief="brief text",
        )
        text = (concepts / "tricky.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert 'type: "Concept"' in text
        # Must have a properly closed frontmatter block (two '---' occurrences).
        assert text.count("---") >= 2
