"""Legacy selection groups carry intent without requiring repeated item fields."""

import pytest

from openkb.agent.document_markdown_prompts import planning_rules
from tests.test_document_planning_locations import _accept


def test_nested_kind_then_action_preserves_classification_and_update_intent():
    result = _accept(
        """## Concepts
### Create
- Consistency
### Update
- Existing
### Related
- Background
## Entities
### Create
- Title: Instrument
  Type: product
### Update
- Title: Unknown product
  Type: product
### Related
- Other product
## Notes
- An incidental signature does not need a page.
""",
        entity_types=["product"],
        existing_targets={"concepts/existing"},
        allowed_update_targets={"concepts/existing"},
        catalog_titles={"concepts/existing": "Existing"},
    )
    assert [(p.title, p.kind, p.target) for p in result.pages] == [
        ("Consistency", "concept", ""),
        ("Existing", "concept", "concepts/existing"),
        ("Instrument", "entity", ""),
    ]
    assert all(not p.subject_ranges for p in result.pages)
    assert [d["reason"] for d in result.deferred_suggestions] == ["update_target_unresolved"]
    assert "Background" in "\n".join(result.batch_notes)
    assert "Other product" in "\n".join(result.batch_notes)


@pytest.mark.parametrize(
    "headings",
    [
        ("## Create concepts", "## Related concepts", "## Create entities"),
        ("## 创建概念", "## 仅关联概念", "## 创建实体"),
        ("## Create\n### Concepts", "## Related\n### Concepts", "## Create\n### Entities"),
    ],
)
def test_combined_and_action_first_groups_do_not_create_related_pages(headings):
    create, related, entities = headings
    result = _accept(
        f"""{create}
- Title: Mechanism
{related}
- Background
{entities}
- Title: Instrument
  Type: product
""",
        entity_types=["product"],
    )
    assert [(p.title, p.kind) for p in result.pages] == [
        ("Mechanism", "concept"),
        ("Instrument", "entity"),
    ]
    assert not result.deferred_suggestions
    assert "Background" in "\n".join(result.batch_notes)


def test_related_only_is_a_valid_empty_selection_and_notes_cannot_create_children():
    result = _accept(
        """## Concepts
### Related
- Background
## Notes
### Create entities
- Title: An unselected example
  Type: product
""",
        entity_types=["product"],
    )
    assert not result.pages and not result.deferred_suggestions
    assert result.no_pages and result.usable
    assert "An unselected example" in "\n".join(result.batch_notes)


def test_all_source_formats_share_the_same_selection_task():
    generic = planning_rules("pages")
    assert generic == planning_rules("pages", navigation_style="legacy_pdf")
    assert "supplied KB stage" in generic
    assert "Concepts" in generic and "Entities" in generic
    assert "Related" in generic
    assert "source locations are not required" in generic
    assert "page bodies" in generic


@pytest.mark.parametrize("entity", ["Instrument (product)", "Instrument（type: product）"])
def test_grouped_inline_purpose_and_parenthetical_type_are_received(entity):
    result = _accept(
        f"""## Concepts
### Create
- **Consistency** — Purpose: A reusable mechanism.
## Entities
### Create
- **{entity}**— Purpose: The central named product.
""",
        entity_types=["product"],
    )
    assert [(p.title, p.kind, p.type, p.purpose) for p in result.pages] == [
        ("Consistency", "concept", None, "A reusable mechanism."),
        ("Instrument", "entity", "product", "The central named product."),
    ]
    assert not result.deferred_suggestions


def test_grouped_plan_without_locations_finishes_without_a_replanning_call(tmp_path):
    from openkb.agent.source_protocol import request_payload
    from tests.test_document_global_planning import run_global

    requests = []

    def respond(messages, *, settings):
        task = request_payload(messages)
        requests.append(task["subtask"])
        if task["subtask"] == "overview":
            return "Instrument calibration and its operating principles."
        return """## Concepts
### Create
- Calibration principles
## Entities
### Create
- Title: Instrument
  Type: product
## Related concepts
- Existing background
"""

    result = run_global(tmp_path, respond)
    assert requests == ["overview", "pages"]
    assert result.outcome == "complete"
    assert (
        result.plan.overview.text.strip() == "Instrument calibration and its operating principles."
    )
    assert [(p.title, p.kind) for p in result.plan.pages] == [
        ("Calibration principles", "concept"),
        ("Instrument", "entity"),
    ]
    assert all(not p.subject_ranges for p in result.plan.pages)


def test_compact_labelled_type_and_nested_subject_preserve_handoff():
    result = _accept(
        """# Concepts
## Create
- **Mechanism** — Purpose: Explain the operation.
  - Subject: section:run
# Entities
## Create
- **Instrument** — Type: product — Purpose: The central named product.
  - Subject: section:run
""",
        entity_types=["product"],
    )
    assert [(p.title, p.type, p.subject_ranges) for p in result.pages] == [
        ("Mechanism", None, [[1, 2]]),
        ("Instrument", "product", [[1, 2]]),
    ]
    assert result.pages[1].purpose == "The central named product."
    assert not result.deferred_suggestions


def test_empty_group_notice_stays_a_note_but_explicit_title_is_preserved():
    result = _accept(
        """# Concepts
## Update
- （未发现已有 concept 页面）
# Entities
## Update
- （未发现已有 entity 页面）
# Concepts
## Create
- Title: （未发现已有 concept 页面）
  Purpose: A deliberately explicit title.
"""
    )
    assert [p.title for p in result.pages] == ["（未发现已有 concept 页面）"]
    assert not result.deferred_suggestions
    assert "（未发现已有 entity 页面）" in "\n".join(result.batch_notes)


def test_path_heading_with_bulleted_fields_keeps_one_named_page():
    result = _accept(
        """## Create pages
### concepts/operation
- Kind: concept
- Title: Operating principles
- Purpose: Explain the reusable mechanism.
- Relevant sections: section:run
"""
    )
    assert len(result.pages) == 1 and not result.deferred_suggestions
    page = result.pages[0]
    assert page.title == "Operating principles"
    assert page.purpose == "Explain the reusable mechanism."
    assert page.subject_ranges == [[1, 2]]


@pytest.mark.parametrize("clue", ["全文", "全书", "whole document", "entire document"])
def test_explicit_whole_source_hint_is_not_an_unknown_location(clue):
    result = _accept(f"## Concepts\n### Create\n- Title: Instrument\n  Subject: {clue}\n")
    assert result.pages[0].subject_ranges == [[0, 2]]
