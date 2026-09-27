"""Flexible candidate wording preserves retrieval and recommendation intent."""

import json

import pytest

from openkb.agent.document_page_resolution import prepare_page
from tests.test_document_page_resolution import _source
from tests.test_document_planning_bindings import accept_bound, request
from tests.test_document_planning_semantics import accept


@pytest.mark.parametrize(
    "label,role",
    [
        ("Location clue", "related"),
        ("Location clues", "related"),
        ("Location hint", "related"),
        ("Location hints", "related"),
        ("Additional location clues", "related"),
        ("Selection", "subject"),
    ],
)
def test_location_word_forms_keep_their_role_and_read_the_original_alias(
    kb_dir, tmp_path, label, role
):
    source, parsed, reader = _source(kb_dir, tmp_path)
    messages, aliases = request(source, parsed, reader, [(1, 7, 23)])
    result = accept_bound(
        f"| Title | Kind | {label} |\n|---|---|---|\n| Procedure | concept | {aliases[0]} |",
        source,
        parsed,
        reader,
        messages,
    )
    page = result.pages[0]
    assert [hint["role"] for hint in page.location_hints] == [role]
    prepared = prepare_page(page, source, parsed, None, reader)
    assert prepared.page.state == "ready"
    assert [block["text"] for block in prepared.evidence["blocks"]] == ["the access token"]


@pytest.mark.parametrize(
    "label", ["Purpose / notes", "用途 / 备注", "**Ｐｕｒｐｏｓｅ ／ ｎｏｔｅｓ**"]
)
def test_composite_purpose_keeps_its_full_limitations_in_purpose_and_notes(label):
    purpose = "Describe calibration; the external procedure has not been checked."
    result = accept(
        f"| Title | Kind | {label} |\n|---|---|---|\n| Calibration | concept | {purpose} |"
    )
    assert result.pages[0].purpose == purpose
    assert purpose in result.pages[0].planning_notes


@pytest.mark.parametrize(
    "title,clues", [("既有标题", "补充线索"), ("Existing title", "Additional location clues")]
)
def test_existing_title_table_adds_locations_once_without_creating_a_page(title, clues):
    pages = accept('{"title":"Calibration","kind":"concept","related":"Preparation"}').pages
    raw = f"| {title} | {clues} |\n|---|---|\n| Calibration | Procedure |"
    first = accept(raw, accepted=pages)
    second = accept(raw, accepted=pages)
    assert not first.pages and not first.rejected and not first.deferred_suggestions
    assert [hint["value"] for hint in pages[0].location_hints] == ["Preparation", "Procedure"]
    assert second.filtered[0]["reason"] == "accepted_echo"


def test_ambiguous_existing_title_is_retained_without_choosing_a_platform():
    pages = accept(
        json.dumps(
            [
                {"title": "Setup", "kind": "concept", "purpose": "Linux only", "section": "Linux"},
                {
                    "title": "Setup",
                    "kind": "concept",
                    "purpose": "Windows only",
                    "section": "Windows",
                },
            ]
        )
    ).pages
    result = accept(
        "| 既有标题 | 类型 | 补充线索 |\n|---|---|---|\n| Setup | concept | Shared recovery |",
        accepted=pages,
    )
    assert not result.pages and not result.rejected
    assert result.deferred_suggestions[0]["reason"] == "extension_target_unresolved"
    assert all(len(page.location_hints) == 1 for page in pages)


@pytest.mark.parametrize("heading", ["### Notes and qualifications", "Notes:", "**Notes:**"])
def test_explanation_lists_remain_notes_while_later_named_candidates_are_accepted(heading):
    result = accept(
        "## Concepts\n\n| Title | Kind |\n|---|---|\n| Calibration | concept |\n\n"
        f"{heading}\n"
        "- Scope — preserve the distinct versions\n"
        "- Source limitations: the external procedure is unread\n\n"
        "| Title | Kind |\n|---|---|\n| Maintenance | concept |\n\n"
        "- Title: Recovery\n  Kind: concept"
    )
    assert {page.title for page in result.pages} == {"Calibration", "Maintenance", "Recovery"}
    assert not result.deferred_suggestions
    assert result.batch_notes == [
        "Scope — preserve the distinct versions",
        "Source limitations: the external procedure is unread",
    ]


@pytest.mark.parametrize(
    "purpose",
    [
        "仅在知识库维护作者索引时建立人物页，否则不单独建页。",
        "Create a person page only if the knowledge base tracks document authors.",
    ],
)
def test_conditional_page_recommendation_is_deferred_but_operation_prerequisite_is_not(purpose):
    result = accept(
        json.dumps(
            [
                {"title": "Author", "kind": "entity", "type": "person", "purpose": purpose},
                {
                    "title": "Calibration",
                    "kind": "concept",
                    "purpose": "Before operating, calibrate the device.",
                },
            ]
        )
    )
    assert [page.title for page in result.pages] == ["Calibration"]
    assert result.deferred_suggestions[0]["reason"] == "conditional_recommendation"
    assert result.deferred_suggestions[0]["purpose"] == purpose


def test_explicit_no_page_and_runtime_condition_stay_explanations_without_topic_blacklists():
    result = accept(
        json.dumps(
            [
                {"title": "Signature", "kind": "concept", "purpose": "仅作署名，不建议独立建页。"},
                {
                    "title": "Extraction",
                    "kind": "concept",
                    "purpose": "仅说明本次解析 source_conditions 的 OCR 未运行诊断，不是原文知识。",
                },
                {
                    "title": "OCR engine",
                    "kind": "concept",
                    "purpose": "The source explains its OCR recognition pipeline.",
                },
                {
                    "title": "Budget planning",
                    "kind": "concept",
                    "purpose": "The source defines the annual budget method.",
                },
            ]
        )
    )
    assert [page.title for page in result.pages] == ["OCR engine", "Budget planning"]
    assert len(result.batch_notes) == 2


def test_extension_does_not_promote_a_conditionally_recommended_author():
    previous = accept(
        '{"title":"Author","kind":"entity","type":"person",'
        '"purpose":"Only create a page if the KB tracks authors."}'
    )
    result = accept(
        '{"Existing title":"Author","kind":"entity","type":"person","related":"Revision history"}',
        deferred=previous.deferred_suggestions,
    )
    assert not result.pages
    assert result.deferred_suggestions[0]["reason"] == "conditional_recommendation"


def test_explicitly_lifted_condition_promotes_extension_and_keeps_prior_origins():
    prior = accept(
        '{"title":"Author","kind":"entity","type":"person",'
        '"purpose":"Only create a page if the KB tracks authors."}'
    )
    result = accept(
        '{"Existing title":"Author",'
        '"purpose":"Condition met. Explicitly recommend an author page.",'
        '"related":"Revision history"}',
        deferred=prior.deferred_suggestions,
    )
    assert len(result.pages) == 1 and not result.deferred_suggestions
    page = result.pages[0]
    assert (page.kind, page.type) == ("entity", "person")
    assert prior.deferred_suggestions[0]["purpose"] in page.planning_notes
    assert result.promoted_suggestions[0]["deferred_key"] == prior.deferred_suggestions[0]["key"]
