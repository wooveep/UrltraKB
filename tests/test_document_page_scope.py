"""Original reads, not planning intent, determine the evidence scope handed downstream."""

import json

from openkb.agent.document_page_resolution import prepare_page
from openkb.agent.document_plan import PagePlan
from tests.test_document_page_resolution import _page, _source


def navigation(source, parsed):
    return {
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "nodes": [
            {"id": name.lower(), "title": name, "parent": None, "start": start, "end": start + 2}
            for name, start in [("Credentials", 0), ("Install", 2), ("Appendix", 4)]
        ],
    }


def test_related_clues_are_read_even_after_subject_hit_and_unknowns_preserve_evidence(
    kb_dir, tmp_path
):
    source, parsed, reader = _source(kb_dir, tmp_path)
    page = _page(
        [
            {"role": "subject", "value": "Install"},
            {"role": "related", "value": "Credentials"},
            {"role": "related", "value": "Missing clue"},
        ]
    )
    prepared = prepare_page(page, source, parsed, navigation(source, parsed), reader)
    assert prepared.page.state == "ready"
    assert "Obtain the access token first." in [row["text"] for row in prepared.occurrences]
    scope = prepared.page.evidence_scope
    assert scope["status"] == "partial"
    assert scope["unresolved_hints"] == [
        {"role": "related", "value": "Missing clue", "reason": "unresolved_location"}
    ]
    assert {row["section_key"] for row in scope["read_sections"] if row["extent"] == "full"} == {
        "section:credentials",
        "section:install",
    }
    assert (
        PagePlan.from_dict(json.loads(json.dumps(prepared.page.to_dict()))).evidence_scope == scope
    )


def test_local_characters_do_not_claim_whole_section_or_expand_a_complete_procedure_title(
    kb_dir, tmp_path
):
    source, parsed, reader = _source(kb_dir, tmp_path)
    page = _page([])
    page.title = "Complete installation procedure"
    page.subject_ranges = [{"block_index": 3, "start_char": 4, "end_char": 9}]
    prepared = prepare_page(page, source, parsed, navigation(source, parsed), reader)
    assert prepared.page.state == "ready"
    assert [row["text"] for row in prepared.occurrences] == ["setup"]
    assert prepared.page.evidence_scope["read_sections"] == [
        {"section_key": "section:install", "heading_path": ["Install"], "extent": "partial"}
    ]


def test_missing_required_location_retains_useful_reads_and_empty_subject_skips(kb_dir, tmp_path):
    source, parsed, reader = _source(kb_dir, tmp_path)
    for role in ("subject", "context"):
        page = _page(
            [{"role": "subject", "value": "Install"}, {"role": role, "value": "Missing section"}]
        )
        prepared = prepare_page(page, source, parsed, None, reader)
        assert prepared.page.state == "ready"
        assert prepared.page.evidence_scope["status"] == "partial"
    missing = prepare_page(
        _page([{"role": "subject", "value": "Missing section"}]), source, parsed, None, reader
    )
    assert missing.page.state == "skipped"
    assert missing.page.evidence_scope["status"] == "unavailable"


def test_title_fallback_and_old_plan_do_not_assume_complete_evidence(kb_dir, tmp_path):
    source, parsed, reader = _source(kb_dir, tmp_path)
    page = _page([])
    page.title = "Install"
    assert PagePlan.from_dict(page.to_dict()).evidence_scope is None
    prepared = prepare_page(page, source, parsed, None, reader)
    assert prepared.page.state == "ready"
    assert prepared.page.evidence_scope["status"] == "unassessed"
    restored = PagePlan.from_dict(json.loads(json.dumps(prepared.page.to_dict())))
    repeated = prepare_page(restored, source, parsed, None, reader)
    assert repeated.page.evidence_scope["status"] == "unassessed"


def test_model_supplied_scope_is_not_accepted_as_a_program_read_receipt():
    from tests.test_document_planning_semantics import accept

    result = accept(
        json.dumps(
            {
                "title": "Install",
                "kind": "concept",
                "evidence_scope": {
                    "protocol": "page-evidence-scope-v1",
                    "status": "located",
                    "read_sections": [],
                    "unresolved_hints": [],
                    "warnings": [],
                },
            }
        )
    )
    assert result.pages[0].evidence_scope is None


def test_related_keyword_alone_does_not_declare_the_complete_page_scope(kb_dir, tmp_path):
    source, parsed, reader = _source(kb_dir, tmp_path)
    page = _page([{"role": "related", "value": "access token"}])
    page.title = "Complete installation"
    prepared = prepare_page(page, source, parsed, navigation(source, parsed), reader)
    assert prepared.page.state == "ready"
    assert prepared.page.evidence_scope["status"] == "unassessed"
    restored = PagePlan.from_dict(json.loads(json.dumps(prepared.page.to_dict())))
    repeated = prepare_page(restored, source, parsed, navigation(source, parsed), reader)
    assert repeated.page.evidence_scope["status"] == "unassessed"


def test_overlapping_subject_context_ranges_charge_each_original_character_once(kb_dir, tmp_path):
    source, parsed, reader = _source(kb_dir, tmp_path)
    page = _page([{"role": "subject", "value": "Install"}, {"role": "context", "value": "Install"}])
    size = parsed.blocks[2].chars + parsed.blocks[3].chars
    prepared = prepare_page(
        page, source, parsed, navigation(source, parsed), reader, max_chars=size
    )
    assert prepared.page.state == "ready"
    assert sum(len(row["text"]) for row in prepared.occurrences) == size
