"""Tests for DocumentPlan orchestration, multi-window accumulation, and checkpoints."""

import json

import pytest

from openkb.agent.document_orchestrator import plan_document
from openkb.agent.document_planning_ledger import DocumentPlanningLedger
from openkb.agent.document_planning_projection import prompt_condition_template
from openkb.agent.document_window_receipts import accepted_window_receipt
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.navigation_evidence import evidence_descriptor
from openkb.processing import DEFAULT_PROCESSING
from openkb.sources import content_id

OFFLINE_PROCESSING = dict(
    DEFAULT_PROCESSING,
    context_tokens=128_000,
    max_context_tokens=128_000,
    output_tokens=4_096,
    max_output_tokens=4_096,
    concurrency=2,
)


def test_planning_reencodes_verified_legacy_navigation_before_budgeting(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _DummyParsed(1)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    old_value = {
        "protocol": "source-prefix-v1",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "start": 0,
        "end": 1,
    }
    record = {
        "schema": 1,
        "source_id": source.source_id,
        "version": source.id,
        "parse": parsed.id,
        "profile": content_id("old profile"),
        "status": "basic",
        "reason": None,
        "usage": {},
        "pageindex": content_id("old pageindex"),
        "windows": [
            {
                "evidence": {"id": content_id(old_value), **old_value},
                "target_start": 0,
                "target_end": 1,
                "status": "complete",
                "reason": None,
                "target_tokens": 1000,
            }
        ],
    }
    navigation = {**record, "id": content_id(record)}

    class BudgetReached(Exception):
        pass

    def inspect_budget_input(_source, _parsed, windows, _limits, *, prompt_tokens):
        assert windows[0]["evidence"] == evidence_descriptor(source, parsed, 0, 1)
        assert navigation["windows"][0]["evidence"]["protocol"] == "source-prefix-v1"
        assert prompt_tokens > 0
        raise BudgetReached

    monkeypatch.setattr("openkb.agent.document_windowing.bounded_windows", inspect_budget_input)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with pytest.raises(BudgetReached):
            plan_document(tmp_path, workspace, source, parsed, navigation, settings, checkpoints)


def test_ledger_persistence_and_proof_share_unicode_canonical_json(tmp_path):
    import hashlib

    from openkb.agent.document_planning_ledger_integrity import canonical_json, plan_state_digest

    source, parsed = _DummySource(), _DummyParsed(1)
    settings = {"model": "openai/offline-test", "processing": OFFLINE_PROCESSING}
    payload = {"甲": 2, "乙": {"z": 1, "a": "值"}}
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "c" * 64)
        try:
            ledger._set_meta("overview", payload)
            stored = ledger.db.execute("SELECT value FROM meta WHERE name = 'overview'").fetchone()[
                0
            ]
            assert stored == canonical_json(payload) == '{"乙":{"a":"值","z":1},"甲":2}'
            digest = hashlib.sha256()
            for table in ("pages", "source_only", "unresolved", "resolutions"):
                digest.update(table.encode("ascii") + b"\n")
            digest.update(b"overview\n")
            digest.update(stored.encode("utf-8") + b"\n")
            assert plan_state_digest(ledger) == digest.hexdigest()
        finally:
            ledger.close()


@pytest.mark.parametrize(
    "invalid_ranges",
    [([0, 1],), [{"block_index": 0, "start_char": 0, "end_char": 1, "extra": True}]],
)
def test_windowing_and_protocol_reject_the_same_malformed_target_ranges(invalid_ranges):
    from openkb.agent.document_range_validation import target_intervals as protocol_intervals
    from openkb.agent.document_windowing import target_intervals as window_intervals

    parsed = _DummyParsed(1)
    window = {"target_start": 0, "target_end": 1, "target_ranges": invalid_ranges}
    with pytest.raises(ValueError):
        protocol_intervals(0, 1, target_ranges=invalid_ranges, block_chars=[parsed.blocks[0].chars])
    with pytest.raises(ValueError):
        window_intervals(window, parsed)


def test_planning_admission_uses_a_constant_parser_condition_envelope():
    template = prompt_condition_template(
        [
            {
                "kind": "parsing_limitation",
                "reason": f"Gap {index}",
                "ranges": [[index, index + 1]],
            }
            for index in range(10_000)
        ]
    )

    assert template == [
        {
            "kind": "parsing_limitation",
            "reason": "Target-specific parser limitations are supplied separately.",
            "ranges": [[0, 1]],
        }
    ]


def test_unlocated_parser_gap_keeps_long_document_metadata_bounded():
    from openkb.agent.document_planning_support import planning_metadata, source_conditions

    source = _DummySource()
    parsed = _DummyParsed(0)
    parsed.blocks = [_DummyBlock(0, "x")] * 100_000
    parsed.quality = [{"status": "needs_review", "reason": "parser output unavailable"}]
    metadata = planning_metadata(
        source=source,
        parsed=parsed,
        navigation=None,
        catalog_window="",
        catalog_targets=set(),
        catalog_entries=[],
        schema="",
        language=None,
        rules="",
        windowing="",
        implementation={},
        recovery_key="recovery",
        contract={},
        entity_types=[],
    )

    assert metadata["parser_omissions"][0]["ranges"] == [[0, 100_000]]
    assert source_conditions(parsed)[0]["ranges"] == [[0, 100_000]]
    assert len(json.dumps(metadata["parser_omissions"])) < 1_000


def test_planning_admission_uses_the_initial_shared_completion_reservation(monkeypatch):
    from openkb.agent.document_planning_support import planning_admission_limits
    from openkb.model_capabilities import ModelCapabilities
    from openkb.processing import RequestLimits

    monkeypatch.setattr(
        "openkb.processing_limits.selected_model_capabilities",
        lambda *_: ModelCapabilities(1_000_000, 900_000, 300_000, False),
    )
    limits = RequestLimits.from_config(
        {
            "model": "known",
            "processing": {
                **DEFAULT_PROCESSING,
                "context_tokens": 100_000,
                "max_context_tokens": 200_000,
                "output_tokens": 4_096,
            },
        }
    )

    admitted = planning_admission_limits(limits)

    assert admitted.output_tokens == 4_096
    assert admitted.input_capacity == 95_904














def test_accepted_ledger_cannot_skip_unfinished_windows(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(6)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    windows = _make_mock_windows(parsed)

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "a" * 64)
        try:
            ledger.initialize(wiki=workspace / "wiki", source=source, parsed=parsed)
            with ledger._transaction():
                receipt = accepted_window_receipt(
                    windows[0],
                    {"accepted": "first window"},
                    checkpoint=None,
                    attempt=0,
                    cached=False,
                    request="b" * 64,
                    predecessor="c" * 64,
                    delta="d" * 64,
                    dispatch_output_tokens=None,
                )
                ledger.db.execute(
                    "INSERT INTO receipts(sequence, payload) VALUES (?, ?)",
                    (1, json.dumps(receipt)),
                )
                ledger._set_meta("windows", windows)
                ledger._set_meta("completed", 1)
                ledger._set_meta("status", "accepted")
                ledger._refresh_integrity()

            assert not ledger.recovery_valid(
                windows, 1, checkpoints.dispatch_output_tokens, parsed=parsed, source=source
            )
        finally:
            ledger.close()


def test_skipped_window_is_durably_settled_without_an_accepted_content_proof(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(6)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    windows = _make_mock_windows(parsed)

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "e" * 64)
        try:
            ledger.initialize(wiki=workspace / "wiki", source=source, parsed=parsed)
            ledger.bind_metadata({"protocol": "document-plan-v2"})
            omission = ledger.settle_skipped(
                windows[0],
                windows=windows,
                completed=1,
                reason="response_empty",
                attempts=3,
                predecessor="c" * 64,
                diagnostic_ref=None,
            )
            assert omission.reason == "response_empty"
            assert ledger.progress() == ("pending", windows, 1)
            assert ledger.receipt(1)["status"] == "skipped"
            assert ledger.receipt(1).get("result") is None
            assert ledger.recovery_valid(
                windows, 1, checkpoints.dispatch_output_tokens, parsed=parsed, source=source
            )
            assert ledger.materialize().planning_omissions == [omission]
            for index, window in enumerate(windows[1:], start=2):
                ledger.settle_skipped(
                    window,
                    windows=windows,
                    completed=index,
                    reason="response_empty",
                    attempts=3,
                    predecessor=ledger.state_digest(),
                    diagnostic_ref=None,
                )
            ledger.mark_accepted(windows)
            assert ledger.recovery_valid(
                windows, len(windows), checkpoints.dispatch_output_tokens,
                parsed=parsed, source=source,
            )
            assert ledger.overview().status == "partial"
            ledger.db.execute("UPDATE planning_omissions SET payload = ? WHERE key = ?", (
                json.dumps({**omission.to_dict(), "reason": "forged"}), omission.key,
            ))
            ledger.db.commit()
            assert not ledger.recovery_valid(
                windows, len(windows), checkpoints.dispatch_output_tokens,
                parsed=parsed, source=source,
            )
        finally:
            ledger.close()


def test_accepted_ledger_requires_routes_for_every_completed_target(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(6)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    windows = _make_mock_windows(parsed)

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "e" * 64)
        try:
            ledger.initialize(wiki=workspace / "wiki", source=source, parsed=parsed)
            with ledger._transaction():
                for sequence, window in enumerate(windows, start=1):
                    receipt = accepted_window_receipt(
                        window,
                        {"accepted": sequence},
                        checkpoint=None,
                        attempt=0,
                        cached=False,
                        request="b" * 64,
                        predecessor="c" * 64,
                        delta="d" * 64,
                        dispatch_output_tokens=None,
                    )
                    ledger.db.execute(
                        "INSERT INTO receipts(sequence, payload) VALUES (?, ?)",
                        (sequence, json.dumps(receipt)),
                    )
                ledger._set_meta(
                    "overview",
                    {
                        "text": "A forged terminal overview.",
                        "ranges": [[0, 6]],
                        "limitations": [],
                        "status": "complete",
                    },
                )
                ledger._set_meta("windows", windows)
                ledger._set_meta("completed", len(windows))
                ledger._set_meta("status", "accepted")
                ledger._refresh_integrity()

            assert not ledger.recovery_valid(
                windows,
                len(windows),
                checkpoints.dispatch_output_tokens,
                parsed=parsed,
                source=source,
            )
        finally:
            ledger.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [("purpose", "FORGED PURPOSE"), ("quality", "published")],
)
def test_recovery_rejects_recomputed_accepted_page_delta(tmp_path, field, value):
    """A fresh mutable-ledger digest cannot replace an immutable accepted delta."""

    from openkb.sources import content_id

    source, parsed = _DummySource(), _DummyParsed(2)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    windows = [
        {
            "evidence": None,
            "target_start": 0,
            "target_end": 2,
            "status": "complete",
            "reason": "",
            "target_tokens": 1_000,
        }
    ]
    decoded = {
        "overview": {"text": "Original overview.", "ranges": [[0, 2]], "limitations": []},
        "page_changes": [
            {
                "target_key": "p1",
                "kind": "concept",
                "type": None,
                "name": "concepts/original",
                "title": "Original",
                "purpose": "original purpose",
                "target": "",
                "subject_ranges": [[0, 2]],
                "necessary_context": [],
            }
        ],
        "source_only": [],
        "unresolved": [],
        "resolutions": [],
    }
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "g" * 64)
        try:
            ledger.initialize(wiki=workspace / "wiki", source=source, parsed=parsed)
            receipt = accepted_window_receipt(
                windows[0],
                {"accepted": "original delta"},
                checkpoint=None,
                attempt=0,
                cached=False,
                request="b" * 64,
                predecessor="c" * 64,
                delta=content_id(decoded),
                dispatch_output_tokens=None,
            )
            ledger.apply_accepted(decoded, receipt, final_window=True, windows=windows, completed=1)
            assert ledger.recovery_valid(
                windows, 1, checkpoints.dispatch_output_tokens, parsed=parsed, source=source
            )
            with ledger._transaction():
                payload = json.loads(
                    ledger.db.execute("SELECT payload FROM pages WHERE key = 'p1'").fetchone()[0]
                )
                payload[field] = value
                ledger.db.execute(
                    "UPDATE pages SET payload = ? WHERE key = 'p1'", (json.dumps(payload),)
                )
                ledger._refresh_integrity()
            assert not ledger.recovery_valid(
                windows, 1, checkpoints.dispatch_output_tokens, parsed=parsed, source=source
            )
        finally:
            ledger.close()


def test_recovery_rejects_plan_rows_before_the_first_accepted_window(tmp_path):
    """A mutable state digest cannot add a pre-acceptance page to an empty ledger."""

    from openkb.agent.document_plan import PagePlan

    source, parsed = _DummySource(), _DummyParsed(1)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    windows = [
        {
            "evidence": None,
            "target_start": 0,
            "target_end": 1,
            "status": "complete",
            "reason": "",
            "target_tokens": 1_000,
        }
    ]
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "h" * 64)
        try:
            ledger.initialize(wiki=workspace / "wiki", source=source, parsed=parsed)
            ledger.replace_schedule(windows, 0)
            assert ledger.recovery_valid(
                windows, 0, checkpoints.dispatch_output_tokens, parsed=parsed, source=source
            )
            with ledger._transaction():
                ledger._store_page(
                    PagePlan(
                        key="p999",
                        kind="concept",
                        name="concepts/forged",
                        title="Forged",
                        purpose="A forged pre-acceptance page.",
                        subject_ranges=[[0, 1]],
                        quality="planned",
                    )
                )
                ledger._refresh_integrity()
            assert not ledger.recovery_valid(
                windows, 0, checkpoints.dispatch_output_tokens, parsed=parsed, source=source
            )
        finally:
            ledger.close()


def test_recovery_rejects_relabelled_baseline_catalog(tmp_path):
    """An original wiki page cannot be made to look like a source addition."""

    from openkb.sources import content_id

    source, parsed = _DummySource(), _DummyParsed(1)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    concepts = workspace / "wiki" / "concepts"
    concepts.mkdir(parents=True)
    baseline = concepts / "baseline.md"
    baseline.write_text("Original catalogue brief.", encoding="utf-8")
    windows = [
        {
            "evidence": None,
            "target_start": 0,
            "target_end": 1,
            "status": "complete",
            "reason": "",
            "target_tokens": 1_000,
        }
    ]

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "i" * 64)
        try:
            ledger.initialize(wiki=workspace / "wiki", source=source, parsed=parsed)
            ledger.replace_schedule(windows, 0)
            assert ledger.recovery_valid(
                windows, 0, checkpoints.dispatch_output_tokens, parsed=parsed, source=source
            )

            changed = "Rewritten catalogue brief."
            baseline.write_text(changed, encoding="utf-8")
            changed_brief = "- baseline: " + changed
            with ledger._transaction():
                ledger.db.execute(
                    "UPDATE catalog SET brief = ?, digest = ?, baseline = 0 WHERE target = ?",
                    (changed_brief, content_id(changed_brief), "concepts/baseline"),
                )
                ledger._set_meta("catalog_snapshot", ledger._catalog_snapshot())
                ledger._set_meta("catalog_count", ledger.catalog_count())
                ledger._refresh_integrity()

            # Neither mutable ownership validation nor the immutable baseline
            # proof may accept a forged reclassification.
            assert not ledger.verify_catalog(wiki=workspace / "wiki", source=source)
            assert not ledger.recovery_valid(
                windows, 0, checkpoints.dispatch_output_tokens, parsed=parsed, source=source
            )
        finally:
            ledger.close()


def test_catalog_recovery_rejects_an_unowned_added_catalogue_target(tmp_path):
    """A forged mutable row cannot present a newly created file as a plan target."""

    from openkb.sources import content_id

    source, parsed = _DummySource(), _DummyParsed(1)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    concepts = workspace / "wiki" / "concepts"
    concepts.mkdir(parents=True)
    external = concepts / "external.md"
    windows = [
        {
            "evidence": None,
            "target_start": 0,
            "target_end": 1,
            "status": "complete",
            "reason": "",
            "target_tokens": 1_000,
        }
    ]

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "j" * 64)
        try:
            ledger.initialize(wiki=workspace / "wiki", source=source, parsed=parsed)
            ledger.replace_schedule(windows, 0)
            external.write_text("External page.", encoding="utf-8")
            brief = "- external: External page."
            with ledger._transaction():
                ledger.db.execute(
                    "INSERT INTO catalog(target, brief, digest, baseline) VALUES (?, ?, ?, 0)",
                    ("concepts/external", brief, content_id(brief)),
                )
                ledger._refresh_integrity()

            assert not ledger.verify_catalog(wiki=workspace / "wiki", source=source)
        finally:
            ledger.close()


def test_catalog_recovery_rejects_an_invalid_added_catalogue_baseline(tmp_path):
    """Every durable catalogue row has exactly one trusted ownership class."""

    from openkb.agent.document_plan import PagePlan
    from openkb.sources import content_id

    source, parsed = _DummySource(), _DummyParsed(1)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    concepts = workspace / "wiki" / "concepts"
    concepts.mkdir(parents=True)
    external = concepts / "external.md"

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "k" * 64)
        try:
            ledger.initialize(wiki=workspace / "wiki", source=source, parsed=parsed)
            external.write_text(
                f"<!-- openkb-source:{source.source_id} -->\nExternal page.", encoding="utf-8"
            )
            brief = "- external: <!-- openkb-source:" + source.source_id + " --> External page."
            with ledger._transaction():
                ledger._store_page(
                    PagePlan(
                        key="p1",
                        kind="concept",
                        name="concepts/external",
                        title="External",
                        purpose="A source-owned addition.",
                        subject_ranges=[[0, 1]],
                    )
                )
                ledger.db.execute(
                    "INSERT INTO catalog(target, brief, digest, baseline) VALUES (?, ?, ?, 0)",
                    ("concepts/external", brief, content_id(brief)),
                )
                ledger._refresh_integrity()
            assert ledger.verify_catalog(wiki=workspace / "wiki", source=source)

            # Simulate an older/tampered SQLite file that predates the schema
            # CHECK; ordinary current writes cannot create this value.
            ledger.db.execute("PRAGMA ignore_check_constraints = ON")
            try:
                with ledger._transaction():
                    ledger.db.execute(
                        "UPDATE catalog SET baseline = 2 WHERE target = ?",
                        ("concepts/external",),
                    )
                    ledger._refresh_integrity()
            finally:
                ledger.db.execute("PRAGMA ignore_check_constraints = OFF")
            assert not ledger.verify_catalog(wiki=workspace / "wiki", source=source)
        finally:
            ledger.close()


def test_recovery_rejects_tampered_output_split_frozen_evidence(tmp_path):
    from openkb.agent.document_planning_ledger_integrity import save_accepted_proof
    from openkb.agent.document_windowing import split_planning_target
    from openkb.sources import content_id

    source, parsed = _DummySource(), _DummyParsed(2)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    parent = {
        "evidence": None,
        "target_start": 0,
        "target_end": 2,
        "status": "complete",
        "reason": "",
        "target_tokens": 1000,
    }
    children = split_planning_target(parent, parsed)

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "f" * 64)
        try:
            ledger.initialize(wiki=workspace / "wiki", source=source, parsed=parsed)
            with ledger._transaction():
                receipt = accepted_window_receipt(
                    children[0],
                    {"accepted": "first split"},
                    checkpoint=None,
                    attempt=0,
                    cached=False,
                    request="b" * 64,
                    predecessor="c" * 64,
                    delta="d" * 64,
                    dispatch_output_tokens=None,
                )
                ledger.db.execute(
                    "INSERT INTO receipts(sequence, payload) VALUES (?, ?)",
                    (1, json.dumps(receipt)),
                )
                ledger._set_meta("windows", children)
                ledger._set_meta("completed", 1)
                ledger._set_meta("status", "pending")
                ledger._refresh_integrity()
            save_accepted_proof(ledger, receipt, 1)

            assert ledger.recovery_valid(
                children,
                1,
                checkpoints.dispatch_output_tokens,
                parsed=parsed,
                source=source,
                base_windows=[parent],
            )
            tampered = [dict(window) for window in children]
            wrong_w = [
                {
                    "block_index": 0,
                    "start_char": 0,
                    "end_char": parsed.blocks[0].chars,
                }
            ]
            tampered[1]["frozen_ranges"] = wrong_w
            tampered[1]["frozen_evidence_id"] = content_id(wrong_w)
            tampered[1]["window_id"] = content_id(
                {
                    "frozen_evidence": tampered[1]["frozen_evidence_id"],
                    "target_ranges": tampered[1]["target_ranges"],
                }
            )
            with ledger._transaction():
                ledger._set_meta("windows", tampered)
                ledger._refresh_integrity()

            assert not ledger.recovery_valid(
                tampered,
                1,
                checkpoints.dispatch_output_tokens,
                parsed=parsed,
                source=source,
                base_windows=[parent],
            )
            tampered[1]["frozen_ranges"] = [
                {
                    "block_index": 1,
                    "start_char": 0,
                    "end_char": parsed.blocks[1].chars,
                }
            ]
            tampered[1]["frozen_evidence_id"] = content_id(tampered[1]["frozen_ranges"])
            tampered[1]["window_id"] = content_id(
                {
                    "frozen_evidence": tampered[1]["frozen_evidence_id"],
                    "target_ranges": tampered[1]["target_ranges"],
                }
            )
            with ledger._transaction():
                ledger._set_meta("windows", tampered)
                ledger._refresh_integrity()

            assert not ledger.recovery_valid(
                tampered,
                1,
                checkpoints.dispatch_output_tokens,
                parsed=parsed,
                source=source,
                base_windows=[parent],
            )
        finally:
            ledger.close()


def test_reloaded_window_schedule_remains_bound_to_its_initial_frozen_window():
    from openkb.agent.document_planning_support import valid_window_schedule
    from openkb.agent.document_windowing import reload_planning_target, split_planning_target
    from openkb.sources import content_id

    source, parsed = _DummySource(), _DummyParsed(2)
    parent = {
        "evidence": evidence_descriptor(source, parsed, 0, 2),
        "target_start": 0,
        "target_end": 2,
        "status": "complete",
        "reason": "",
        "target_tokens": 1_000,
    }
    children = reload_planning_target(source, parsed, parent)

    assert valid_window_schedule(children, parsed, source=source, base_schedule=[parent])
    # Recursive reloads retain the root receipt instead of becoming an
    # unbound descendant of a transient child descriptor.
    grandchildren = reload_planning_target(source, parsed, children[0])
    assert valid_window_schedule(
        [*grandchildren, children[1]], parsed, source=source, base_schedule=[parent]
    )
    children[0]["reloaded_from"] = "not-the-initial-window"
    assert not valid_window_schedule(children, parsed, source=source, base_schedule=[parent])

    # A range hash is descriptive metadata, not a root W receipt. A descendant
    # may only name the initial immutable descriptor/receipt as its parent.
    root_span_hash = content_id(
        [
            {"block_index": index, "start_char": 0, "end_char": block.chars}
            for index, block in enumerate(parsed.blocks)
        ]
    )
    forged = [dict(window) for window in grandchildren]
    for window in forged:
        window["reloaded_from"] = root_span_hash
        window["window_id"] = content_id(
            {"reloaded_from": root_span_hash, "target_ranges": window["target_ranges"]}
        )
    assert not valid_window_schedule(
        [*forged, children[1]], parsed, source=source, base_schedule=[parent]
    )

    # Output pressure can subsequently split a reloaded descriptor.  Its
    # canonical frozen-W/T child IDs remain bound to the original descriptor,
    # rather than being mistaken for a forged reload receipt.
    source, parsed = _DummySource(), _DummyParsed(3)
    parent = {
        "evidence": evidence_descriptor(source, parsed, 0, 3),
        "target_start": 0,
        "target_end": 3,
        "status": "complete",
        "reason": "",
        "target_tokens": 1_000,
    }
    children = reload_planning_target(source, parsed, parent)
    candidate = next(child for child in children if child["target_end"] - child["target_start"] > 1)
    split = split_planning_target(candidate, parsed)
    assert valid_window_schedule(
        [child for child in children if child is not candidate] + split,
        parsed,
        source=source,
        base_schedule=[parent],
    )


class _DummySource:
    def __init__(self):
        self.source_id = "a" * 32
        self.id = "b" * 64
        self.name = "multi_doc.md"
        self.origin = "multi_doc.md"


class _DummyBlock:
    def __init__(self, idx, text=""):
        self.id = f"{idx:064x}"
        self.order = idx
        self.text = text
        self.chars = len(text) or 100
        self.kind = "paragraph"
        self.location = {}
        self.assets = []
        self.context = ""


class _DummyParsed:
    def __init__(self, num_blocks=6):
        self.id = "c" * 64
        self.blocks = [_DummyBlock(i, f"Content of block {i}. ") for i in range(num_blocks)]
        self.quality = []


def _make_mock_windows(parsed):
    # Two windows: 0..3 and 3..6
    source = _DummySource()
    w1_descriptor = evidence_descriptor(source, parsed, 0, 3)
    w2_descriptor = evidence_descriptor(source, parsed, 3, 6)
    return [
        {
            "evidence": w1_descriptor,
            "target_start": 0,
            "target_end": 3,
            "status": "complete",
            "reason": "",
            "target_tokens": 1000,
        },
        {
            "evidence": w2_descriptor,
            "target_start": 3,
            "target_end": 6,
            "status": "complete",
            "reason": "",
            "target_tokens": 1000,
        },
    ]


@pytest.mark.parametrize("described_tokens", [1_000_000, 10_000_000])
def test_large_source_descriptors_keep_exact_bounded_durable_windows(tmp_path, described_tokens):
    """Scale metadata without allocating synthetic source prose in memory."""
    import tracemalloc

    from openkb.agent.document_window_schedule import valid_window_schedule
    from openkb.agent.document_windowing import bounded_windows
    from openkb.processing import RequestLimits

    source, parsed = _DummySource(), _DummyParsed(1)
    parsed.blocks[0].text = ""
    parsed.blocks[0].chars = 2 * described_tokens
    original = {
        "evidence": evidence_descriptor(source, parsed, 0, 1),
        "target_start": 0,
        "target_end": 1,
        "status": "complete",
        "reason": "",
        "target_tokens": described_tokens,
    }
    settings = {"model": "openai/offline-test", "processing": OFFLINE_PROCESSING}
    limits = RequestLimits.from_config(settings)

    tracemalloc.start()
    try:
        schedule, _ = bounded_windows(source, parsed, [original], limits, prompt_tokens=1_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(schedule) > 1
    assert peak < 20 * 1024 * 1024
    assert all(len(json.dumps(window)) < 1_000 for window in schedule)
    ranges = [window["target_ranges"][0] for window in schedule]
    assert ranges[0]["start_char"] == 0
    assert ranges[-1]["end_char"] == parsed.blocks[0].chars
    assert all(left["end_char"] == right["start_char"] for left, right in zip(ranges, ranges[1:]))
    assert valid_window_schedule(schedule, parsed, source=source, base_schedule=[original])
    forged = [{**schedule[0], "derived_from": "f" * 64}, *schedule[1:]]
    assert not valid_window_schedule(forged, parsed, source=source, base_schedule=[original])

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        ledger = DocumentPlanningLedger(checkpoints, "d" * 64)
        ledger.replace_schedule(schedule, 0)
        ledger.close()
        restored = DocumentPlanningLedger(checkpoints, "d" * 64)
        try:
            assert restored.progress() == ("pending", schedule, 0)
            assert restored.path.stat().st_size < 2_000_000
        finally:
            restored.close()


def test_plan_document_rejects_an_incomplete_navigation_manifest(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(2)
    navigation = {
        "id": "nav-incomplete",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "complete",
        "windows": [
            {
                "evidence": evidence_descriptor(source, parsed, 0, 1),
                "target_start": 0,
                "target_end": 1,
                "status": "complete",
                "reason": "",
                "target_tokens": 1000,
            }
        ],
        "nodes": [],
    }
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with pytest.raises(ValueError, match="Incomplete navigation window coverage"):
            plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                navigation,
                settings,
                checkpoints,
                mock_caller=lambda *_args, **_kwargs: pytest.fail(
                    "invalid navigation reached planner"
                ),
            )






def test_navigation_hints_follow_sparse_target_and_budget():
    from types import SimpleNamespace

    from openkb.agent.document_planning_support import select_navigation_hints

    navigation = {
        "nodes": [
            {"start": index, "title": f"Section {index}", "summary": "x" * 1000}
            for index in range(26)
        ]
    }
    hints = select_navigation_hints(
        navigation, [[0, 2], [24, 26]], SimpleNamespace(input_capacity=3000)
    )
    titles = {hint["title"] for hint in hints}
    assert titles <= {"Section 0", "Section 1", "Section 24", "Section 25"}
    assert {"Section 0", "Section 25"} <= titles
    assert len(json.dumps(hints, ensure_ascii=False)) <= 800
















def test_context_basis_must_quote_frozen_source_text():
    from openkb.agent.document_planning_support import (
        canonicalize_context_bases,
        fallback_read_evidence,
    )

    source, parsed = _DummySource(), _DummyParsed(1)
    decoded = {
        "page_changes": [
            {"necessary_context": [{"basis": "invented prose", "basis_ranges": [[0, 1]]}]}
        ],
        "resolutions": [],
    }

    with pytest.raises(ValueError, match="quote frozen source evidence"):
        canonicalize_context_bases(
            decoded,
            fallback_read_evidence(source, parsed, 0, 1),
            parsed,
        )


def test_published_resolved_dependency_evidence_is_referenced_coverage():
    from dataclasses import asdict

    from openkb.compilation_report import CompileReport
    from openkb.evidence import Evidence
    from openkb.source_coverage import source_coverage

    source, parsed = _DummySource(), _DummyParsed(1)
    report = CompileReport()
    report.source_occurrences["p1:o1"] = {
        "reference": asdict(
            Evidence(
                source.source_id,
                source.id,
                parsed.id,
                parsed.blocks[0].id,
                0,
                parsed.blocks[0].chars,
            )
        ),
        # Coverage has exactly four public routes. The generator may retain a
        # finer internal route marker, but resolved evidence is context-only
        # in the source-coverage account.
        "route": "context_only",
        "reason": "resolved_dependency_evidence",
        "page": "concepts/core-system",
    }
    report.published_occurrences.add("p1:o1")

    coverage = source_coverage(source, parsed, report, published=True)

    assert coverage["status"] == "complete"
    assert coverage["ranges"][0]["status"] == "referenced"
    assert coverage["ranges"][0]["reason"] == "resolved_dependency_evidence"
















def _two_window_navigation(source, parsed, identifier):
    return {
        "id": identifier,
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "complete",
        "windows": [
            {
                "evidence": evidence_descriptor(source, parsed, 0, 1),
                "target_start": 0,
                "target_end": 1,
                "status": "complete",
                "reason": "",
                "target_tokens": 1000,
            },
            {
                "evidence": evidence_descriptor(source, parsed, 1, 2),
                "target_start": 1,
                "target_end": 2,
                "status": "complete",
                "reason": "",
                "target_tokens": 1000,
            },
        ],
        "nodes": [],
    }


def test_recovery_schedule_rejects_target_ranges_outside_their_window():
    from openkb.agent.document_planning_support import valid_window_schedule

    source, parsed = _DummySource(), _DummyParsed(2)
    windows = _two_window_navigation(source, parsed, "nav-swapped-targets")["windows"]
    windows[0]["target_ranges"] = [[1, 2]]
    windows[1]["target_ranges"] = [[0, 1]]

    assert not valid_window_schedule(windows, parsed, source=source)
    windows = _two_window_navigation(source, parsed, "nav-swapped-evidence")["windows"]
    windows[0]["evidence"] = evidence_descriptor(source, parsed, 1, 2)
    windows[1]["evidence"] = evidence_descriptor(source, parsed, 0, 1)

    assert not valid_window_schedule(windows, parsed, source=source)


def test_recovery_schedule_binds_every_base_window_and_its_receipt_identity():
    """A resumed W/T list cannot invent a fresh partial range or receipt ID."""

    from openkb.agent.document_planning_support import valid_window_schedule
    from openkb.sources import content_id

    source, parsed = _DummySource(), _DummyParsed(1)

    def partial(start, end):
        ranges = [{"block_index": 0, "start_char": start, "end_char": end}]
        return {
            "evidence": None,
            "target_start": 0,
            "target_end": 1,
            "target_ranges": ranges,
            "status": "complete",
            "reason": "",
            "target_tokens": 1_000,
            "window_id": content_id(
                {
                    "source_id": source.source_id,
                    "version_id": source.id,
                    "parse_id": parsed.id,
                    "ranges": ranges,
                }
            ),
        }

    end = parsed.blocks[0].chars
    first, second = end // 3, 2 * end // 3
    base = [partial(0, first), partial(first, second), partial(second, end)]
    assert valid_window_schedule(base, parsed, source=source, base_schedule=base)

    # The altered intervals still cover the source exactly, but they were not
    # present in the frozen schedule that the accepted-prefix receipt binds.
    assert not valid_window_schedule(
        [partial(0, first), partial(first, second - 1), partial(second - 1, end)],
        parsed,
        source=source,
        base_schedule=base,
    )

    descriptor = evidence_descriptor(source, parsed, 0, 1)
    forged = {
        "evidence": descriptor,
        "target_start": 0,
        "target_end": 1,
        "status": "complete",
        "reason": "",
        "target_tokens": 1_000,
        "window_id": "forged",
    }
    assert not valid_window_schedule(
        [forged], parsed, source=source, base_schedule=[{**forged, "window_id": descriptor["id"]}]
    )

    # A descriptor W with frozen output children must retain an exact movable
    # T.  Without it, a persisted arbitrary window ID could enter the accepted
    # receipt/cache identity while still covering the entire descriptor.
    frozen_without_target = {
        **forged,
        "frozen_ranges": [{"block_index": 0, "start_char": 0, "end_char": parsed.blocks[0].chars}],
        "frozen_evidence_id": descriptor["id"],
    }
    assert not valid_window_schedule(
        [frozen_without_target],
        parsed,
        source=source,
        base_schedule=[{**forged, "window_id": descriptor["id"]}],
    )


def _crowded_page_changes(count, ranges):
    return [
        {
            "local_key": f"local-{index}",
            "target_key": "",
            "kind": "concept",
            "name": f"concepts/prior-{index}",
            "title": f"Prior {index}",
            "purpose": "A deliberately large historical page register.",
            "subject_ranges": ranges,
            "necessary_context": [],
        }
        for index in range(1, count + 1)
    ]


def _crowded_planning_counter(*, messages=None, text=None, **_):
    if messages is None:
        return 0
    payload = json.loads(messages[-1]["content"])
    carry = payload.get("carry", {})
    return (
        100
        + 200 * len(carry.get("page_register", []))
        + 200 * len(carry.get("open_references", []))
    )


def _crowded_planning_settings():
    return {
        "model": "mock-model",
        "processing": {
            **OFFLINE_PROCESSING,
            "context_tokens": 10_000,
            "max_context_tokens": 10_000,
            "output_tokens": 1_000,
            "max_output_tokens": 1_000,
        },
    }
