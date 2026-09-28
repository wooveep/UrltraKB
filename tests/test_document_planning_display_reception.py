"""Small reproductions of real Markdown field and section display variants."""

import pytest

from tests.test_document_planning_locations import _accept


def test_explicit_title_agrees_with_decorated_heading_and_related_sources_locate():
    result = _accept("""# Create pages
## concept: glossary
Title: glossary
Kind: concept
Related sources: 范围（section:pre, section:run）
""")
    page = result.pages[0]
    assert page.title == "glossary"
    assert page.subject_ranges == [[0, 1], [1, 2]]
    assert not any("title" in note for note in page.planning_notes)


@pytest.mark.parametrize(
    "field", ["Sections", "Related sources", "Source sections", "Relevant sections"]
)
def test_parenthetical_group_remains_whole_and_keeps_every_key(field):
    result = _accept(f"""# Create pages
## Operation
Kind: concept
{field}: 第三章 3.4.1、3.4.3（section:pre, section:run）
""")
    assert result.pages[0].subject_ranges == [[0, 1], [1, 2]]


def test_multiple_displayed_groups_split_only_between_parentheses():
    page = _accept("""## Create pages
### Operation
Kind: concept
Relevant sections: Prerequisites (section:pre)、Steps and prerequisites（section:run, section:pre）
""").pages[0]
    assert page.subject_ranges == [[0, 1], [1, 2]]


@pytest.mark.parametrize("last", ["section:run", "section:run (operation)"])
def test_each_key_keeps_its_own_parenthetical_description(last):
    page = _accept(f"""# Create pages
## Operation
Kind: concept
Source sections: section:pre (prerequisites, limits), {last}
""").pages[0]
    assert page.subject_ranges == [[0, 1], [1, 2]]


def test_annotated_key_list_cannot_hide_an_unknown_later_selection():
    page = _accept("""# Create pages
## Operation
Kind: concept
Sections: section:run (operation), section:unknown (missing)
""").pages[0]
    assert page.subject_ranges == [[1, 2]]
    assert any("section:unknown" in note for note in page.planning_notes)


def test_title_conflict_is_preserved_when_explicit_title_differs():
    page = _accept("""# Create pages
## concept: Alpha
Title: Beta
Kind: concept
Sections: section:run
""").pages[0]
    assert any("title" in note for note in page.planning_notes)


def test_display_decoration_does_not_override_explicit_classification():
    result = _accept("""# Create pages
## Entity: Instrument mechanism
Title: Instrument mechanism
Kind: concept
Sections: section:run
""")
    assert len(result.pages) == 1
    assert result.pages[0].title == "Instrument mechanism"
    assert result.pages[0].kind == "concept"


def test_punctuation_inside_a_named_field_does_not_start_a_new_page():
    result = _accept("""## Create pages
### Operation
- Kind: concept
- Purpose: Foundational mechanism — used throughout this document.
- Source sections: section:pre (prerequisites), section:run (operation)
- Context: Options are a | b | c — values explained in the source.
""")
    assert len(result.pages) == 1
    assert not result.deferred_suggestions
    assert result.pages[0].purpose == "Foundational mechanism — used throughout this document."
    assert result.pages[0].subject_ranges == [[0, 1], [1, 2]]


@pytest.mark.parametrize("title", ["[[concepts/Operation]]", "[[concepts/operation|Operation]]"])
def test_wiki_title_and_comma_separated_real_heading_list_are_display_formats(title):
    page = _accept(f"""## Create pages
### {title}
- Kind: concept
- Sections: 前提, 操作
""").pages[0]
    assert page.title == "Operation"
    assert page.subject_ranges == [[0, 1], [1, 2]]


def test_comma_in_one_real_heading_is_not_split_into_two_selections():
    from tests.test_document_markdown_planning import _navigation

    navigation = _navigation()
    navigation[0]["heading_path"] = ["前提, 操作"]
    page = _accept(
        """## Create pages
### Operation
Kind: concept
Sections: 前提, 操作
""",
        navigation=navigation,
    ).pages[0]
    assert page.subject_ranges == [[0, 1]]


@pytest.mark.parametrize("field", ["Related sources", "External references"])
def test_external_reference_does_not_invent_a_subject_or_block_the_suggestion(field):
    page = _accept(f"""# Create pages
## Operation
Kind: concept
{field}: 参见《其他手册》安装章节
""").pages[0]
    assert not page.subject_ranges
    assert page.state == "pending_evidence"


def test_unknown_key_inside_display_group_is_not_silently_dropped():
    page = _accept("""# Create pages
## Operation
Kind: concept
Related sources: 操作（section:run, section:unknown）
""").pages[0]
    assert not page.subject_ranges


def test_inline_fields_do_not_reinterpret_titles_annotations_or_quoted_labels():
    page = _accept("""## Create pages
### A title: Another title
Kind: concept
Title: A title: Another title
Purpose: Explain the `Subject: token` field and (Type: product) notation.
Subject: 前提（section_key: section:pre）
""").pages[0]
    assert page.title == "A title: Another title"
    assert page.purpose == "Explain the `Subject: token` field and (Type: product) notation."
    assert page.location_hints == [{"role": "subject", "value": "前提（section_key: section:pre）"}]


def test_displayed_root_key_is_preserved_instead_of_accepting_only_its_child():
    from tests.test_document_markdown_planning import _navigation

    nav = [
        *_navigation(),
        {
            "section_key": "section:root",
            "title": "1 Whole source",
            "heading_path": ["1 Whole source"],
            "original_range": [0, 2],
        },
    ]
    page = _accept(
        """# Create pages
## Operation
Kind: concept
Related sources: 全文（section:root, section:run）
""",
        navigation=nav,
    ).pages[0]
    assert [0, 2] in page.subject_ranges
    unknown = _accept(
        """# Create pages
## Operation
Kind: concept
Sections: 第1章（section:unknown）
""",
        navigation=nav,
    ).pages[0]
    assert not unknown.subject_ranges


def test_page_heading_keeps_paragraphs_and_multiline_location_fields_across_blanks():
    result = _accept(
        """# Create pages

## Concept: glossary
Kind: concept

A reusable mechanism with its prerequisites.

来源章节：
- 前提（section:pre）
- 操作（section:run）

## Entity: Instrument
Kind: product

The central named product.

Related sources:
- 操作（section:run）

# Update pages
## Concept: Existing
Kind: concept

Sections:
- section:run

# Notes
- An incidental author should not have a page.
""",
        entity_types=["product"],
    )
    assert [p.title for p in result.pages] == ["glossary", "Instrument"]
    assert result.pages[0].purpose == "A reusable mechanism with its prerequisites."
    assert result.pages[0].subject_ranges == [[0, 1], [1, 2]]
    assert result.pages[1].subject_ranges == [[1, 2]]
    assert len(result.deferred_suggestions) == 1
    assert result.deferred_suggestions[0]["reason"] == "update_target_unresolved"
    assert any("incidental author" in note for note in result.batch_notes)
