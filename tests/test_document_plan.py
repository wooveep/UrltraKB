"""Tests for DocumentPlan contracts, protocol, range validation, and budget."""

import pytest

from openkb.agent.document_plan import (
    DocumentPlan,
    OverviewPlan,
    PagePlan,
    SourceOnlyItem,
    UnresolvedItem,
    derive_page_states,
    from_dict,
    to_dict,
    validate_plan,
)
from openkb.agent.document_protocol import (
    PLAN_RULES,
    SYSTEM,
    PlanningProjectionRequired,
    calculate_plan_budget,
    decode_plan_response,
    plan_messages,
)


def _make_dummy_parsed(num_blocks=10):
    class DummyBlock:
        def __init__(self, idx, chars=100):
            self.id = f"b_{idx}"
            self.order = idx
            self.chars = chars
            self.kind = "paragraph"
            self.location = {}
            self.assets = []

    class DummyParsed:
        def __init__(self):
            self.id = "parse_1"
            self.blocks = [DummyBlock(i) for i in range(num_blocks)]

    return DummyParsed()


def test_document_plan_dataclasses_and_serialization():
    plan = DocumentPlan(
        metadata={
            "protocol": "document-plan-v1",
            "source_id": "src_1",
            "version_id": "ver_1",
            "parse_id": "parse_1",
            "model": "gpt-4o",
            "status": "pending",
        },
        overview=OverviewPlan(
            text="High level overview of the document.",
            ranges=[[0, 5]],
            limitations=["External appendix not included."],
            status="partial",
        ),
        pages=[
            PagePlan(
                key="p1",
                kind="concept",
                type=None,
                name="concepts/architecture",
                title="System Architecture",
                purpose="Explain system architecture",
                target="",
                subject_ranges=[[0, 3]],
                necessary_context=[
                    {
                        "relation": "applicable_condition",
                        "ranges": [[3, 4]],
                        "basis": "Condition applies to architecture",
                        "basis_ranges": [[3, 4]],
                    }
                ],
                state="ready",
                quality="planned",
            ),
            PagePlan(
                key="p2",
                kind="entity",
                type="Server",
                name="entities/primary-server",
                title="Primary Server",
                purpose="Document the primary server configuration",
                target="",
                subject_ranges=[[4, 6]],
                necessary_context=[],
                state="ready",
                quality="planned",
            ),
        ],
        source_only=[
            SourceOnlyItem(
                ranges=[[6, 7]],
                reason="Administrative metadata and copyright information",
            )
        ],
        unresolved=[
            UnresolvedItem(
                key="u1",
                location=[[2, 3]],
                problem_type="missing_prerequisite",
                missing_target="prerequisite-setup",
                affected_pages=["p1"],
                blocking=True,
                reason="Required prerequisite setup guide is in subsequent section",
                status="open",
            )
        ],
        resolutions=[],
    )

    data = to_dict(plan)
    assert data["metadata"]["protocol"] == "document-plan-v1"
    assert len(data["pages"]) == 2
    assert data["pages"][0]["name"] == "concepts/architecture"
    assert data["unresolved"][0]["blocking"] is True

    restored = from_dict(data)
    assert restored.metadata == plan.metadata
    assert restored.overview.text == plan.overview.text
    assert restored.overview.status == "partial"
    assert len(restored.pages) == 2
    assert restored.pages[1].kind == "entity"
    assert restored.pages[1].type == "Server"
    assert restored.unresolved[0].key == "u1"
    assert restored.unresolved[0].affected_pages == ["p1"]


def test_from_dict_rejects_invalid_durable_container_shapes():
    with pytest.raises(ValueError, match="Invalid DocumentPlan"):
        from_dict(
            {
                "metadata": {"protocol": "document-plan-v1"},
                "overview": {},
                "pages": {},
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        )


def test_derive_page_states():
    pages = [
        PagePlan(
            key="p1",
            kind="concept",
            type=None,
            name="concepts/alpha",
            title="Alpha",
            purpose="Alpha purpose",
            target="",
            subject_ranges=[[0, 2]],
            necessary_context=[],
            state="ready",
        ),
        PagePlan(
            key="p2",
            kind="concept",
            type=None,
            name="concepts/beta",
            title="Beta",
            purpose="Beta purpose",
            target="",
            subject_ranges=[[2, 4]],
            necessary_context=[],
            state="ready",
        ),
    ]
    unresolved = [
        UnresolvedItem(
            key="u1",
            location=[[0, 1]],
            problem_type="missing_dependency",
            missing_target="dep1",
            affected_pages=["p1"],
            blocking=True,
            reason="Missing dep",
            status="open",
        ),
        UnresolvedItem(
            key="u2",
            location=[[2, 3]],
            problem_type="missing_note",
            missing_target="note1",
            affected_pages=["p2"],
            blocking=False,
            reason="Non-blocking reference",
            status="open",
        ),
    ]

    derived = derive_page_states(pages, unresolved)
    assert derived[0].state == "blocked"  # p1 is blocked by u1
    assert derived[1].state == "ready"  # p2 has only non-blocking u2

    # Now mark u1 as resolved
    unresolved[0].status = "resolved"
    derived2 = derive_page_states(pages, unresolved)
    assert derived2[0].state == "ready"


def test_validate_plan_ranges_and_entity_types():
    parsed = _make_dummy_parsed(10)
    allowed_entity_types = ["Service", "Server"]

    # Valid plan
    valid_plan = DocumentPlan(
        metadata={"protocol": "document-plan-v1"},
        overview=OverviewPlan(text="Overview", ranges=[[0, 5]], limitations=[], status="complete"),
        pages=[
            PagePlan(
                key="p1",
                kind="concept",
                name="concepts/c1",
                title="C1",
                purpose="P1",
                target="",
                subject_ranges=[[0, 2]],
                necessary_context=[],
                state="ready",
            ),
            PagePlan(
                key="p2",
                kind="entity",
                type="Server",
                name="entities/e1",
                title="E1",
                purpose="P2",
                target="",
                subject_ranges=[[2, 4]],
                necessary_context=[],
                state="ready",
            ),
        ],
        source_only=[SourceOnlyItem(ranges=[[4, 5]], reason="Metadata")],
        unresolved=[],
        resolutions=[],
    )
    assert validate_plan(valid_plan, parsed, allowed_entity_types, set()) is True

    valid_plan.overview.text = " "
    with pytest.raises(ValueError, match="non-empty overview"):
        validate_plan(valid_plan, parsed, allowed_entity_types, set())
    valid_plan.overview.text = "Overview"
    valid_plan.overview.ranges = []
    with pytest.raises(ValueError, match="overview needs exact evidence ranges"):
        validate_plan(valid_plan, parsed, allowed_entity_types, set())
    valid_plan.overview.ranges = [[0, 5]]
    valid_plan.pages[0].purpose = " "
    with pytest.raises(ValueError, match="non-empty purpose"):
        validate_plan(valid_plan, parsed, allowed_entity_types, set())

    # Invalid range (out of bounds)
    invalid_range_plan = DocumentPlan(
        metadata={"protocol": "document-plan-v1"},
        overview=OverviewPlan(text="Overview", ranges=[[0, 20]], limitations=[]),
        pages=[],
        source_only=[],
        unresolved=[],
        resolutions=[],
    )
    with pytest.raises(ValueError, match="out of bounds"):
        validate_plan(invalid_range_plan, parsed, allowed_entity_types, set())

    # Invalid entity type
    invalid_entity_plan = DocumentPlan(
        metadata={"protocol": "document-plan-v1"},
        overview=OverviewPlan(text="Overview", ranges=[[0, 2]], limitations=[]),
        pages=[
            PagePlan(
                key="p1",
                kind="entity",
                type="UnknownType",
                name="entities/e1",
                title="E1",
                purpose="P1",
                target="",
                subject_ranges=[[0, 2]],
                necessary_context=[],
                state="ready",
            )
        ],
        source_only=[],
        unresolved=[],
        resolutions=[],
    )
    with pytest.raises(ValueError, match="entity type"):
        validate_plan(invalid_entity_plan, parsed, allowed_entity_types, set())


def test_validate_plan_rejects_a_persisted_state_that_disagrees_with_blocking_work():
    parsed = _make_dummy_parsed(2)
    plan = DocumentPlan(
        metadata={"protocol": "document-plan-v1"},
        overview=OverviewPlan(text="Overview", ranges=[[0, 1]], limitations=[], status="complete"),
        pages=[
            PagePlan(
                key="p1",
                kind="concept",
                name="concepts/alpha",
                title="Alpha",
                purpose="Explain alpha.",
                target="",
                subject_ranges=[[0, 1]],
                necessary_context=[],
                state="ready",
            )
        ],
        source_only=[],
        unresolved=[
            UnresolvedItem(
                key="u1",
                location=[[0, 1]],
                problem_type="missing_prerequisite",
                missing_target="A prerequisite",
                affected_pages=["p1"],
                blocking=True,
                reason="The prerequisite is absent.",
                status="open",
            )
        ],
        resolutions=[],
    )

    with pytest.raises(ValueError, match="derived state"):
        validate_plan(plan, parsed, [], set())

    plan.pages[0].state = "blocked"
    assert validate_plan(plan, parsed, [], set()) is True


def test_universal_rules_contain_no_hardcoded_test_strings():
    # Enforce Universal rules requirement (Decision 32):
    # Prompt system and rules must not contain hardcoded test filenames,
    # source hashes, fixed block numbers, specific test commands,
    # expected page counts, or target answers!
    forbidden = [
        "manual.md",
        "small.md",
        "thinking.md",
        "uuid",
        "1e420d",
        "b_0",
        "Section 1",
        "Section 2",
        "start --timeout 42",
        "37 kPa",
    ]
    for term in forbidden:
        assert term.lower() not in SYSTEM.lower(), f"Forbidden term '{term}' found in SYSTEM"
        assert term.lower() not in PLAN_RULES.lower(), (
            f"Forbidden term '{term}' found in PLAN_RULES"
        )


def test_plan_messages_frozen_w_prefix_stability():
    evidence = {
        "group_id": "grp_1",
        "source_id": "src_1",
        "version_id": "ver_1",
        "parse_id": "parse_1",
        "blocks": [{"id": "b0", "text": "Sample block zero"}],
    }

    # First request with target 0..1, empty carry
    msg1 = plan_messages(
        evidence=evidence,
        carry_s={"overview": None, "page_register": [], "open_references": []},
        target_t={"target_start": 0, "target_end": 1},
        navigation_hints=[],
        catalog_window="",
        entity_types=["Service"],
        schema="",
    )

    # Second request with moving target 1..2, non-empty carry
    msg2 = plan_messages(
        evidence=evidence,
        carry_s={
            "overview": "Partial overview",
            "page_register": [{"key": "p1", "name": "c1", "title": "C1"}],
            "open_references": [],
        },
        target_t={"target_start": 1, "target_end": 2},
        navigation_hints=[],
        catalog_window="existing catalogue",
        entity_types=["Service"],
        schema="",
    )

    # System prompts must be identical
    assert msg1[0]["content"] == msg2[0]["content"]
    assert msg1[0]["role"] == "system"

    # User message prefix containing frozen W must be identical!
    # Because encode_payload and share_contexts are called on evidence first.
    user1 = msg1[1]["content"]
    user2 = msg2[1]["content"]
    assert user1.startswith('{"protocol":"source-prefix-v1"')
    assert user2.startswith('{"protocol":"source-prefix-v1"')
    prefix_w = '"evidence":{"group_id":"grp_1"'
    assert prefix_w in user1
    assert prefix_w in user2


def test_decode_plan_response_success():
    raw_response = {
        "overview": {
            "text": "Overview of sections 0 to 2.",
            "ranges": [[0, 2]],
            "limitations": ["Requires external tool"],
        },
        "page_changes": [
            {
                "local_key": "c1",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/data-backup",
                "title": "Data Backup",
                "purpose": "Procedures for regular data backups",
                "subject_ranges": [[0, 1]],
                "necessary_context": [
                    {
                        "relation": "applicable_condition",
                        "ranges": [[1, 2]],
                        "basis": "Backup requires healthy cluster state",
                        "basis_ranges": [[1, 2]],
                    }
                ],
            },
            {
                "local_key": "c2",
                "target_key": "",
                "kind": "entity",
                "type": "Database",
                "name": "entities/prod-db",
                "title": "Production Database",
                "purpose": "Production database specifications",
                "subject_ranges": [[1, 2]],
                "necessary_context": [],
            },
        ],
        "source_only": [
            {
                "ranges": [[2, 3]],
                "reason": "Change log history not relevant to core knowledge",
            }
        ],
        "unresolved": [
            {
                "location": [[0, 1]],
                "problem_type": "missing_prerequisite",
                "missing_target": "auth-tokens",
                "affected_pages": ["c1"],
                "blocking": True,
                "reason": "Backup credentials documented in next chapter",
            }
        ],
        "resolutions": [],
    }

    decoded = decode_plan_response(
        raw_response,
        target_start=0,
        target_end=3,
        total_blocks=10,
        allowed_entity_types=["Database"],
        existing_targets=set(),
        carry_pages=[],
        open_unresolved=[],
    )

    assert decoded["overview"]["text"] == "Overview of sections 0 to 2."
    assert len(decoded["page_changes"]) == 2
    p1 = decoded["page_changes"][0]
    assert p1["kind"] == "concept"
    assert p1["name"] == "concepts/data-backup"
    assert p1["target_key"].startswith("p")
    assert p1["subject_ranges"] == [[0, 1]]

    # Affected pages in unresolved should map local_key "c1" to the newly allocated target_key
    assert decoded["unresolved"][0]["affected_pages"] == [p1["target_key"]]
    assert decoded["unresolved"][0]["key"].startswith("u")


def test_decode_requires_evidence_bound_overview_and_nonempty_page_purpose():
    def response():
        return {
            "overview": {"text": "Current source overview.", "ranges": [[0, 1]], "limitations": []},
            "page_changes": [
                {
                    "local_key": "current",
                    "target_key": "",
                    "kind": "concept",
                    "name": "concepts/current",
                    "title": "Current",
                    "purpose": "Describe the current source material.",
                    "subject_ranges": [[0, 1]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    kwargs = {
        "target_start": 0,
        "target_end": 1,
        "total_blocks": 1,
        "allowed_entity_types": [],
        "existing_targets": set(),
        "carry_pages": [],
        "open_unresolved": [],
    }
    blank_text = response()
    blank_text["overview"]["text"] = " "
    with pytest.raises(ValueError, match="Overview needs non-empty text"):
        decode_plan_response(blank_text, **kwargs)

    missing_ranges = response()
    missing_ranges["overview"]["ranges"] = []
    with pytest.raises(ValueError, match="Overview needs exact evidence ranges"):
        decode_plan_response(missing_ranges, **kwargs)

    blank_purpose = response()
    blank_purpose["page_changes"][0]["purpose"] = " "
    with pytest.raises(ValueError, match="Invalid page purpose"):
        decode_plan_response(blank_purpose, **kwargs)


def test_decode_rejects_attachment_ranges_from_every_model_section():
    """Attachment contents are retained assets, never read planning evidence."""

    def response():
        return {
            "overview": {
                "text": "Readable material overview.",
                "ranges": [[1, 2]],
                "limitations": [],
            },
            "page_changes": [
                {
                    "local_key": "current",
                    "target_key": "",
                    "kind": "concept",
                    "name": "concepts/current",
                    "title": "Current",
                    "purpose": "Describe the readable source material.",
                    "subject_ranges": [[1, 2]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    def attachment_overview(raw):
        raw["overview"]["ranges"] = [[0, 1]]

    def attachment_subject(raw):
        raw["page_changes"][0]["subject_ranges"] = [[0, 1]]

    def attachment_context(raw):
        raw["page_changes"][0]["necessary_context"] = [
            {
                "relation": "applicable_condition",
                "ranges": [[0, 1]],
                "basis": "The attachment says this applies.",
                "basis_ranges": [[1, 2]],
            }
        ]

    def attachment_basis(raw):
        raw["page_changes"][0]["necessary_context"] = [
            {
                "relation": "applicable_condition",
                "ranges": [[1, 2]],
                "basis": "The attachment says this applies.",
                "basis_ranges": [[0, 1]],
            }
        ]

    def attachment_source_only(raw):
        raw["source_only"] = [{"ranges": [[0, 1]], "reason": "Attachment asset."}]

    def attachment_unresolved(raw):
        raw["unresolved"] = [
            {
                "location": [[0, 1]],
                "problem_type": "missing_prerequisite",
                "missing_target": "attachment-details",
                "affected_pages": ["current"],
                "blocking": True,
                "reason": "The attachment is not readable planning evidence.",
            }
        ]

    def attachment_resolution(raw):
        raw["resolutions"] = [{"unresolved_key": "u1", "basis_ranges": [[0, 1]]}]

    checks = [
        (attachment_overview, []),
        (attachment_subject, []),
        (attachment_context, []),
        (attachment_basis, []),
        (attachment_source_only, []),
        (attachment_unresolved, []),
        (attachment_resolution, [{"key": "u1"}]),
    ]
    for mutate, open_unresolved in checks:
        raw = response()
        mutate(raw)
        with pytest.raises(ValueError, match="unread attachment content"):
            decode_plan_response(
                raw,
                target_start=0,
                target_end=2,
                total_blocks=2,
                allowed_entity_types=[],
                existing_targets=set(),
                carry_pages=[],
                open_unresolved=open_unresolved,
                ignored_blocks={0},
            )


def test_decode_assigns_unresolved_keys_after_a_resolved_history_item():
    response = {
        "overview": {"text": "Partial", "ranges": [[0, 1]], "limitations": []},
        "page_changes": [
            {
                "local_key": "new-page",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/current",
                "title": "Current",
                "purpose": "Current target content",
                "subject_ranges": [[0, 1]],
                "necessary_context": [],
            }
        ],
        "source_only": [],
        "unresolved": [
            {
                "location": [[0, 1]],
                "problem_type": "missing_external_material",
                "missing_target": "external-material",
                "affected_pages": ["new-page"],
                "blocking": True,
                "reason": "The external material was not supplied.",
            }
        ],
        "resolutions": [],
    }

    decoded = decode_plan_response(
        response,
        target_start=0,
        target_end=1,
        total_blocks=2,
        allowed_entity_types=[],
        existing_targets=set(),
        carry_pages=[],
        open_unresolved=[],
        known_unresolved_keys={"u1"},
    )

    assert decoded["unresolved"][0]["key"] == "u2"


def test_decode_rejects_necessary_context_outside_frozen_evidence():
    response = {
        "overview": {"text": "Partial", "ranges": [[0, 1]], "limitations": []},
        "page_changes": [
            {
                "local_key": "new-page",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/current",
                "title": "Current",
                "purpose": "Current target content",
                "subject_ranges": [[0, 1]],
                "necessary_context": [
                    {
                        "relation": "applicable_condition",
                        "ranges": [[1, 2]],
                        "basis": "The later block is required.",
                        "basis_ranges": [[0, 1]],
                    }
                ],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }

    with pytest.raises(ValueError, match="supplied frozen evidence"):
        decode_plan_response(
            response,
            target_start=0,
            target_end=1,
            total_blocks=2,
            allowed_entity_types=[],
            existing_targets=set(),
            carry_pages=[],
            open_unresolved=[],
            evidence_ranges=[[0, 1]],
        )


def test_character_range_is_exact_and_cannot_claim_a_future_target():
    response = {
        "overview": {"text": "Partial overview", "ranges": [[0, 1]], "limitations": []},
        "page_changes": [
            {
                "local_key": "p",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/partial",
                "title": "Partial",
                "purpose": "A selected source excerpt",
                "subject_ranges": [
                    {"block_index": 0, "start_char": 0, "end_char": 4},
                    {"block_index": 0, "start_char": 4, "end_char": 10},
                ],
                "necessary_context": [],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }
    decoded = decode_plan_response(
        response,
        target_start=0,
        target_end=1,
        total_blocks=2,
        block_chars=[10, 12],
        allowed_entity_types=[],
        existing_targets=set(),
        carry_pages=[],
        open_unresolved=[],
    )
    assert decoded["page_changes"][0]["subject_ranges"][0]["end_char"] == 4

    response["overview"]["ranges"] = [[0, 2]]
    with pytest.raises(ValueError, match="future target"):
        decode_plan_response(
            response,
            target_start=0,
            target_end=1,
            total_blocks=2,
            block_chars=[10, 12],
            allowed_entity_types=[],
            existing_targets=set(),
            carry_pages=[],
            open_unresolved=[],
        )


def test_overview_cannot_claim_a_future_movable_target_already_present_in_frozen_w():
    response = {
        "overview": {"text": "Premature overview", "ranges": [[1, 2]], "limitations": []},
        "page_changes": [
            {
                "local_key": "current",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/current",
                "title": "Current",
                "purpose": "Current target only",
                "subject_ranges": [[0, 1]],
                "necessary_context": [],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }

    with pytest.raises(ValueError, match="future target"):
        decode_plan_response(
            response,
            target_start=0,
            target_end=1,
            total_blocks=2,
            block_chars=[10, 10],
            allowed_entity_types=[],
            existing_targets=set(),
            carry_pages=[],
            open_unresolved=[],
            target_ranges=[[0, 1]],
            evidence_ranges=[[0, 2]],
        )


def test_partial_target_range_cannot_expand_to_the_rest_of_its_block():
    partial = {"block_index": 0, "start_char": 0, "end_char": 5}
    response = {
        "overview": {"text": "First excerpt", "ranges": [partial], "limitations": []},
        "page_changes": [
            {
                "local_key": "first",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/first",
                "title": "First excerpt",
                "purpose": "The selected first excerpt",
                "subject_ranges": [partial],
                "necessary_context": [],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }
    kwargs = {
        "target_start": 0,
        "target_end": 1,
        "total_blocks": 1,
        "block_chars": [10],
        "allowed_entity_types": [],
        "existing_targets": set(),
        "carry_pages": [],
        "open_unresolved": [],
        "target_ranges": [partial],
    }

    decoded = decode_plan_response(response, **kwargs)
    assert decoded["page_changes"][0]["subject_ranges"] == [partial]

    response["page_changes"][0]["subject_ranges"] = [
        {"block_index": 0, "start_char": 0, "end_char": 6}
    ]
    with pytest.raises(ValueError, match="current target"):
        decode_plan_response(response, **kwargs)

    response["page_changes"][0]["subject_ranges"] = [partial]
    response["overview"]["ranges"] = [[0, 1]]
    with pytest.raises(ValueError, match="unread future target"):
        decode_plan_response(response, **kwargs)


def test_decode_plan_response_rejects_missing_sections_or_invalid_ranges():
    # Missing overview
    with pytest.raises(ValueError, match="missing overview"):
        decode_plan_response(
            {"page_changes": []},
            target_start=0,
            target_end=2,
            total_blocks=10,
            allowed_entity_types=[],
            existing_targets=set(),
            carry_pages=[],
            open_unresolved=[],
        )

    # Invalid range exceeding total_blocks
    with pytest.raises(ValueError, match="out of bounds"):
        decode_plan_response(
            {
                "overview": {"text": "T", "ranges": [[0, 2]], "limitations": []},
                "page_changes": [
                    {
                        "local_key": "c1",
                        "target_key": "",
                        "kind": "concept",
                        "name": "concepts/test",
                        "title": "Test",
                        "purpose": "Purpose",
                        "subject_ranges": [[0, 999]],
                        "necessary_context": [],
                    }
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            },
            target_start=0,
            target_end=2,
            total_blocks=10,
            allowed_entity_types=[],
            existing_targets=set(),
            carry_pages=[],
            open_unresolved=[],
        )


def test_calculate_plan_budget():
    # Capacity 100,000 tokens
    # Output reservation 4,096
    # Margin = max(4096, ceil(100000 * 0.03)) = 4096
    # Fixed overhead = 1000
    # Available = 100000 - 4096 - 4096 - 1000 = 90808
    budget = calculate_plan_budget(
        effective_capacity=100000,
        output_reservation=4096,
        fixed_overhead=1000,
    )
    assert budget["margin"] == 4096
    assert budget["available_for_payload"] == 90808

    # When request fits
    assert budget["fits"](evidence_tokens=20000, carry_tokens=5000, dynamic_tokens=2000) is True

    # When request exceeds capacity
    assert budget["fits"](evidence_tokens=85000, carry_tokens=10000, dynamic_tokens=2000) is False

    # A fixed 4k margin would consume an entire explicitly configured 4k
    # context and prevent even a small source window from being planned.
    small = calculate_plan_budget(
        effective_capacity=4096,
        output_reservation=1024,
        fixed_overhead=0,
    )
    assert small["margin"] == 256
    assert small["available_for_payload"] == 2816


def test_decode_rejects_future_coverage_and_unbound_resolution():
    base = {
        "overview": {"text": "Partial", "ranges": [[0, 1]], "limitations": []},
        "page_changes": [
            {
                "local_key": "new-page",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/partial",
                "title": "Partial",
                "purpose": "Partial scope",
                "subject_ranges": [[2, 3]],
                "necessary_context": [],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }
    with pytest.raises(ValueError, match="current target"):
        decode_plan_response(
            base,
            target_start=0,
            target_end=2,
            total_blocks=4,
            allowed_entity_types=[],
            existing_targets=set(),
            carry_pages=[],
            open_unresolved=[],
        )

    base["page_changes"] = []
    base["resolutions"] = [{"unresolved_key": "u-missing", "basis_ranges": [[0, 1]]}]
    with pytest.raises(ValueError, match="open unresolved"):
        decode_plan_response(
            base,
            target_start=0,
            target_end=2,
            total_blocks=4,
            allowed_entity_types=[],
            existing_targets=set(),
            carry_pages=[],
            open_unresolved=[],
        )

    base["resolutions"] = [{"unresolved_key": "u1", "basis_ranges": []}]
    with pytest.raises(ValueError, match="exact basis_ranges"):
        decode_plan_response(
            base,
            target_start=0,
            target_end=2,
            total_blocks=4,
            allowed_entity_types=[],
            existing_targets=set(),
            carry_pages=[],
            open_unresolved=[{"key": "u1"}],
        )


def test_decode_reserves_projected_page_keys_and_persists_context_basis_ranges():
    response = {
        "overview": {"text": "Partial", "ranges": [[0, 1]], "limitations": []},
        "page_changes": [
            {
                "local_key": "new",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/current",
                "title": "Current",
                "purpose": "Current work",
                "subject_ranges": [[0, 1]],
                "necessary_context": [
                    {
                        "relation": "explicit_reference",
                        "ranges": [[0, 1]],
                        "basis": "The source explicitly names this work.",
                        "basis_ranges": [[0, 1]],
                    }
                ],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }

    decoded = decode_plan_response(
        response,
        target_start=0,
        target_end=1,
        total_blocks=2,
        allowed_entity_types=[],
        existing_targets=set(),
        carry_pages=[],
        open_unresolved=[],
        # p1 belongs to a valid but budget-projected-out prior page.
        known_page_keys={"p1"},
    )

    assert decoded["page_changes"][0]["target_key"] == "p2"
    assert decoded["page_changes"][0]["necessary_context"][0]["basis_ranges"] == [[0, 1]]


def test_decode_promotes_a_new_name_reserved_by_an_unprojected_catalog_page():
    response = {
        "overview": {"text": "Partial", "ranges": [[0, 1]], "limitations": []},
        "page_changes": [
            {
                "local_key": "new",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/unprojected",
                "title": "Unprojected",
                "purpose": "Must not shadow a hidden catalog entry",
                "subject_ranges": [[0, 1]],
                "necessary_context": [],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }
    with pytest.raises(PlanningProjectionRequired) as raised:
        decode_plan_response(
            response,
            target_start=0,
            target_end=1,
            total_blocks=2,
            allowed_entity_types=[],
            existing_targets=set(),
            reserved_targets={"concepts/unprojected"},
            carry_pages=[],
            open_unresolved=[],
        )
    assert raised.value.catalog_target == "concepts/unprojected"


def test_decode_promotes_then_reuses_a_hidden_registered_page_by_its_path():
    response = {
        "overview": {"text": "Current", "ranges": [[0, 1]], "limitations": []},
        "page_changes": [
            {
                "local_key": "extend",
                "target_key": "",
                "target": "concepts/existing",
                "kind": "concept",
                "name": "concepts/existing",
                "title": "Existing",
                "purpose": "Extend the existing page with current evidence.",
                "subject_ranges": [[0, 1]],
                "necessary_context": [],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }
    kwargs = {
        "target_start": 0,
        "target_end": 1,
        "total_blocks": 1,
        "allowed_entity_types": [],
        "existing_targets": {"concepts/existing"},
        "known_page_keys": {"p1"},
        "known_page_names": {"concepts/existing"},
        "known_page_name_keys": {"concepts/existing": "p1"},
        "open_unresolved": [],
    }
    with pytest.raises(ValueError, match="required planning identity"):
        decode_plan_response(response, carry_pages=[], **kwargs)

    decoded = decode_plan_response(
        response,
        carry_pages=[
            {
                "key": "p1",
                "kind": "concept",
                "type": None,
                "name": "concepts/existing",
                "title": "Existing",
                "purpose": "Existing page purpose.",
                "target": "concepts/existing",
            }
        ],
        **kwargs,
    )
    assert decoded["page_changes"][0]["target_key"] == "p1"


def test_decode_rejects_malformed_source_only_unresolved_and_context_contracts():
    base = {
        "overview": {"text": "Partial", "ranges": [[0, 1]], "limitations": []},
        "page_changes": [],
        "source_only": [{"ranges": [[0, 1]], "reason": 1}],
        "unresolved": [],
        "resolutions": [],
    }
    kwargs = {
        "target_start": 0,
        "target_end": 1,
        "total_blocks": 2,
        "allowed_entity_types": [],
        "existing_targets": set(),
        "carry_pages": [],
        "open_unresolved": [],
    }
    with pytest.raises(ValueError, match="source_only"):
        decode_plan_response(base, **kwargs)

    base["source_only"] = []
    base["unresolved"] = [
        {
            "location": [[0, 1]],
            "problem_type": "unresolved_cross_reference",
            "missing_target": "missing procedure",
            "affected_pages": ["p1"],
            "reason": "The procedure was not supplied.",
        }
    ]
    with pytest.raises(ValueError, match="Invalid unresolved fields"):
        decode_plan_response(base, **kwargs)

    base["unresolved"][0]["blocking"] = False
    with pytest.raises(ValueError, match="cannot be downgraded"):
        decode_plan_response(base, **kwargs)

    base["unresolved"] = []
    base["page_changes"] = [
        {
            "local_key": "p",
            "target_key": "",
            "kind": "concept",
            "name": "concepts/current",
            "title": "Current",
            "purpose": "Current work",
            "subject_ranges": [[0, 1]],
            "necessary_context": [
                {
                    "relation": "applicable_condition",
                    "ranges": [[0, 1]],
                    "basis": "This condition applies.",
                }
            ],
        }
    ]
    with pytest.raises(ValueError, match="Invalid necessary_context element"):
        decode_plan_response(base, **kwargs)


def test_decode_rejects_unrepresentable_page_changes_at_the_protocol_boundary():
    response = {
        "overview": {"text": "Partial", "ranges": [[0, 1]], "limitations": []},
        "page_changes": [
            {
                "local_key": "p",
                "target_key": "",
                "kind": "concept",
                "name": "concepts/current",
                "title": "Current",
                "purpose": "Current work",
                "subject_ranges": [[0, 1]],
                "necessary_context": [
                    {
                        "relation": "applicable_condition",
                        "ranges": [[0, 1]],
                        "basis": "This condition applies.",
                        "basis_ranges": [[0, 1]],
                    }
                ],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }
    kwargs = {
        "target_start": 0,
        "target_end": 1,
        "total_blocks": 1,
        "allowed_entity_types": [],
        "existing_targets": set(),
        "carry_pages": [],
        "open_unresolved": [],
    }

    response["page_changes"][0]["necessary_context"][0]["unexpected"] = "field"
    with pytest.raises(ValueError, match="Invalid necessary_context element"):
        decode_plan_response(response, **kwargs)

    del response["page_changes"][0]["necessary_context"][0]["unexpected"]
    response["page_changes"][0]["type"] = 7
    with pytest.raises(ValueError, match="concept page cannot have an entity type"):
        decode_plan_response(response, **kwargs)


def test_from_dict_rejects_malformed_nested_page_records():
    payload = {
        "metadata": {"protocol": "document-plan-v1"},
        "overview": {"text": "", "ranges": [], "limitations": [], "status": "partial"},
        "pages": [
            {
                "key": "p1",
                "kind": "concept",
                "name": "concepts/current",
                "title": "Current",
                "subject_ranges": "not-a-list",
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }

    with pytest.raises(ValueError, match="Invalid DocumentPlan"):
        from_dict(payload)
