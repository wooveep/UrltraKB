"""Request-local aliases retain their original source slices after planning."""

import json
from dataclasses import asdict

import pytest

from openkb.agent.document_page_resolution import prepare_page
from openkb.agent.document_plan import PagePlan
from openkb.agent.document_planning_response import accept_pages
from openkb.agent.document_protocol import plan_messages
from openkb.evidence import Evidence
from tests.test_document_page_resolution import _source


def request(source, parsed, reader, slices):
    blocks = []
    for index, start, end in slices:
        block = parsed.blocks[index]
        ref = Evidence(source.source_id, source.id, parsed.id, block.id, start, end)
        view = reader.read(ref, max_chars=10000)
        blocks.append({"id": block.id, "order": index, "reference": asdict(ref), "text": view.text})
    evidence = {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "group_id": "test-window",
        "blocks": blocks,
    }
    messages = plan_messages(evidence, {}, {}, [], "", [], "")
    aliases = [row["id"] for row in json.loads(messages[-1]["content"])["evidence"]["blocks"]]
    return messages, aliases


def accept_bound(raw, source, parsed, reader, messages, *, accepted=None):
    from openkb.agent.document_planning_bindings import capture_request

    binding = capture_request(messages, source, parsed, {"window": "test"}, reader.sources)
    return accept_pages(
        raw,
        navigation=[],
        target=[[0, len(parsed.blocks)]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
        accepted=accepted,
        source_identity=source.source_id,
        request_binding=binding,
    )


def test_same_alias_in_two_windows_binds_two_original_blocks_after_round_trip(kb_dir, tmp_path):
    source, parsed, reader = _source(kb_dir, tmp_path)
    first, ids1 = request(source, parsed, reader, [(1, 0, parsed.blocks[1].chars)])
    second, ids2 = request(source, parsed, reader, [(3, 0, parsed.blocks[3].chars)])
    assert ids1 == ids2
    pages = []
    for messages, aliases in [(first, ids1), (second, ids2)]:
        result = accept_bound(
            f"- Title: Setup\n  Kind: concept\n  Section: {aliases[0]}",
            source,
            parsed,
            reader,
            messages,
            accepted=pages,
        )
        pages.extend(result.pages)
    assert len(pages) == 1 and pages[0].state == "pending_evidence"
    restored = PagePlan.from_dict(json.loads(json.dumps(pages[0].to_dict())))
    prepared = prepare_page(restored, source, parsed, None, reader)
    assert prepared.page.state == "ready"
    assert {row["text"] for row in prepared.evidence["blocks"]} == {
        "Obtain the access token first.",
        "Run setup after obtaining credentials.",
    }


def test_alias_bound_to_partial_block_does_not_expand_to_whole_block(kb_dir, tmp_path):
    source, parsed, reader = _source(kb_dir, tmp_path)
    messages, aliases = request(source, parsed, reader, [(1, 7, 23)])
    page = accept_bound(
        f"- Title: Token\n  Kind: concept\n  Section: {aliases[0]}",
        source,
        parsed,
        reader,
        messages,
    ).pages[0]
    prepared = prepare_page(page, source, parsed, None, reader)
    assert prepared.page.state == "ready"
    assert prepared.page.subject_ranges == [{"block_index": 1, "start_char": 7, "end_char": 23}]
    assert prepared.evidence["blocks"][0]["text"] == "the access token"


@pytest.mark.parametrize("orders,expected", [([1, 2, 3], "ready"), ([1, 3], "skipped")])
def test_alias_range_requires_the_intervening_original_content(kb_dir, tmp_path, orders, expected):
    source, parsed, reader = _source(kb_dir, tmp_path)
    messages, aliases = request(
        source, parsed, reader, [(i, 0, parsed.blocks[i].chars) for i in orders]
    )
    page = accept_bound(
        f"- Title: Procedure\n  Kind: concept\n  Section: block {aliases[0]}–{aliases[-1]}",
        source,
        parsed,
        reader,
        messages,
    ).pages[0]
    result = prepare_page(page, source, parsed, None, reader)
    assert result.page.state == expected
    if expected == "skipped":
        assert not result.occurrences and page.location_hints[0]["value"]["unresolved"]
    else:
        assert len(result.occurrences) == 3


@pytest.mark.parametrize("role", ["Section", "Related"])
def test_unknown_alias_preserves_known_part_only_for_optional_clues(kb_dir, tmp_path, role):
    source, parsed, reader = _source(kb_dir, tmp_path)
    messages, aliases = request(source, parsed, reader, [(1, 0, parsed.blocks[1].chars)])
    page = accept_bound(
        f"- Title: Procedure\n  Kind: concept\n  {role}: {aliases[0]}, @e:absent",
        source,
        parsed,
        reader,
        messages,
    ).pages[0]
    result = prepare_page(page, source, parsed, None, reader)
    assert result.page.state == ("skipped" if role == "Section" else "ready")
    assert any("@e:absent" in note for note in result.page.planning_notes)


def test_missing_request_map_cannot_use_another_windows_aliases(kb_dir, tmp_path):
    source, parsed, reader = _source(kb_dir, tmp_path)
    messages, aliases = request(source, parsed, reader, [(3, 0, parsed.blocks[3].chars)])
    result = accept_pages(
        json.dumps(
            {
                "title": "Old hint",
                "kind": "concept",
                "section": {
                    "from_block": aliases[0],
                    "through_block": aliases[0],
                },
            }
        ),
        navigation=[],
        target=[[0, 6]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
        evidence=json.loads(messages[-1]["content"])["evidence"],
    )
    assert len(result.pages) == 1 and not result.pages[0].subject_ranges
    prepared = prepare_page(result.pages[0], source, parsed, None, reader)
    assert prepared.page.state == "skipped"


def test_modified_bound_extent_is_an_integrity_failure(kb_dir, tmp_path):
    source, parsed, reader = _source(kb_dir, tmp_path)
    messages, aliases = request(source, parsed, reader, [(1, 7, 23)])
    page = accept_bound(
        f"- Title: Token\n  Kind: concept\n  Section: {aliases[0]}",
        source,
        parsed,
        reader,
        messages,
    ).pages[0]
    page.location_hints[0]["value"]["ranges"][0]["start_char"] = 0
    with pytest.raises(ValueError, match="original request"):
        prepare_page(page, source, parsed, None, reader)


def test_replayed_parent_alias_uses_its_saved_mapping_in_a_different_child_request(
    kb_dir, tmp_path
):
    from openkb.agent.document_planning_bindings import capture_request, load_binding

    source, parsed, reader = _source(kb_dir, tmp_path)
    parent, aliases = request(source, parsed, reader, [(1, 0, parsed.blocks[1].chars)])
    child, child_aliases = request(source, parsed, reader, [(3, 0, parsed.blocks[3].chars)])
    assert aliases == child_aliases
    original = capture_request(
        parent, source, parsed, {"target_start": 0, "target_end": 6}, reader.sources
    )
    restored = load_binding(reader.sources, original["id"], source, parsed)
    result = accept_pages(
        f"- Title: Parent suggestion\n  Kind: concept\n  Section: {aliases[0]}",
        navigation=[],
        target=[[3, 4]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
        source_identity=source.source_id,
        request_binding=restored,
        evidence=json.loads(child[-1]["content"])["evidence"],
    )
    prepared = prepare_page(result.pages[0], source, parsed, None, reader)
    assert [row["text"] for row in prepared.evidence["blocks"]] == [
        "Obtain the access token first."
    ]


@pytest.mark.parametrize(
    "value",
    [
        "{alias}; section:missing",
        "{alias}, Unknown heading",
        "{alias}、Unknown heading",
        ["{alias}", "Unknown chapter"],
        {"from_block": "{alias}", "through_block": "missing"},
    ],
)
def test_alias_cannot_hide_an_unresolved_required_selection(kb_dir, tmp_path, value):
    source, parsed, reader = _source(kb_dir, tmp_path)
    messages, aliases = request(source, parsed, reader, [(1, 0, parsed.blocks[1].chars)])
    value = json.loads(json.dumps(value).replace("{alias}", aliases[0]))
    page = accept_bound(
        json.dumps({"title": "Procedure", "kind": "concept", "section": value}),
        source,
        parsed,
        reader,
        messages,
    ).pages[0]
    assert prepare_page(page, source, parsed, None, reader).page.state == "skipped"


@pytest.mark.parametrize("bound", [True, False])
@pytest.mark.parametrize("separator", ["; ", ", ", "、"])
def test_mixed_optional_hint_keeps_the_stable_heading_with_or_without_alias_map(
    kb_dir, tmp_path, bound, separator
):
    from openkb.agent.document_planning_bindings import capture_request

    source, parsed, reader = _source(kb_dir, tmp_path)
    messages, aliases = request(source, parsed, reader, [(1, 0, parsed.blocks[1].chars)])
    binding = capture_request(messages, source, parsed, {}, reader.sources) if bound else None
    page = accept_pages(
        json.dumps(
            {"title": "Procedure", "kind": "concept", "related": f"{aliases[0]}{separator}Install"}
        ),
        navigation=[],
        target=[[0, 6]],
        parsed=parsed,
        entity_types=[],
        existing_targets=set(),
        source_identity=source.source_id,
        request_binding=binding,
    ).pages[0]
    prepared = prepare_page(page, source, parsed, None, reader)
    assert prepared.page.state == "ready"
    assert any("Run setup" in row["text"] for row in prepared.evidence["blocks"])
