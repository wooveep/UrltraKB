"""Tests for DocumentPlan orchestration, multi-window accumulation, and checkpoints."""

import json
import sqlite3

import pytest

from openkb.agent.compilation_index import artifact_summaries
from openkb.agent.document_orchestrator import plan_document
from openkb.agent.document_plan import to_dict
from openkb.agent.document_planning_ledger import DocumentPlanningLedger
from openkb.agent.document_planning_projection import prompt_condition_template
from openkb.agent.document_window_receipts import accepted_window_receipt
from openkb.agent.evidence_checkpoints import CompilationCheckpoints
from openkb.navigation_evidence import evidence_descriptor
from openkb.processing import DEFAULT_PROCESSING, ProcessingIncomplete, processing_scope

OFFLINE_PROCESSING = dict(
    DEFAULT_PROCESSING,
    context_tokens=128_000,
    max_context_tokens=128_000,
    output_tokens=4_096,
    max_output_tokens=4_096,
    concurrency=2,
)


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


def test_length_expansion_readmits_and_resizes_planning_window(tmp_path, monkeypatch):
    import litellm

    from tests.test_adaptive_processing import response

    source, parsed = _DummySource(), _DummyParsed(2)
    for index, block in enumerate(parsed.blocks):
        block.text = f"Readable source block {index}."
    settings = {
        "model": "mock-model",
        "processing": {
            **OFFLINE_PROCESSING,
            "context_tokens": 1_000,
            "max_context_tokens": 1_000,
            "output_tokens": 100,
            "max_output_tokens": 200,
        },
    }
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)

    def token_counter(*, messages=None, **_):
        if not messages:
            return 0
        payload = json.loads(messages[-1]["content"])
        blocks = payload.get("evidence", {}).get("blocks", [])
        if not blocks:
            return 0
        return 890 if len(blocks) > 1 else 500

    def read_target(_kb_dir, _source, _parsed, descriptor, target_ranges):
        if target_ranges:
            from openkb.agent.document_planning_support import fallback_target_evidence

            return fallback_target_evidence(source, parsed, target_ranges)
        from openkb.agent.document_planning_support import fallback_read_evidence

        return fallback_read_evidence(source, parsed, descriptor["start"], descriptor["end"])

    calls = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        calls.append(kwargs)
        if len(calls) == 1:
            return response(truncated=True, tokens=kwargs["max_tokens"])
        target = payload["target"]
        start, end = target["target_start"], target["target_end"]
        return response(
            {
                "overview": {
                    "text": "Complete" if end == len(parsed.blocks) else "Partial",
                    "ranges": [[start, end]],
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": f"page-{start}",
                        "target_key": "",
                        "kind": "concept",
                        "name": f"concepts/block-{start}",
                        "title": f"Block {start}",
                        "purpose": "Retained after capacity readmission.",
                        "subject_ranges": [[start, end]],
                        "necessary_context": [],
                    }
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            },
            tokens=kwargs["max_tokens"],
        )

    monkeypatch.setattr(litellm, "token_counter", token_counter)
    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr("openkb.agent.document_planning_support.read_target_evidence", read_target)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with processing_scope(settings):
            plan = plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                None,
                settings,
                checkpoints,
            )

    assert [call["max_tokens"] for call in calls] == [100, 200, 200]
    assert plan.metadata["status"] == "accepted"
    assert [page.subject_ranges for page in plan.pages] == [[[0, 1]], [[1, 2]]]


def test_empty_document_persists_a_complete_overview_across_resume(tmp_path):
    source = _DummySource()
    parsed = _DummyParsed(0)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        initial = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            mock_caller=lambda *_args, **_kwargs: pytest.fail("empty document must not call model"),
        )
        assert initial.metadata["status"] == "accepted"
        assert initial.overview.status == "complete"

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        resumed = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            resume=True,
            mock_caller=lambda *_args, **_kwargs: pytest.fail(
                "resume must use the accepted ledger"
            ),
        )
    assert resumed.metadata["status"] == "accepted"
    assert resumed.overview.status == "complete"


def test_plan_document_closes_ledger_on_success_and_exception(tmp_path, monkeypatch):
    source, parsed = _DummySource(), _DummyParsed(0)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    ledgers = []
    closed = []
    fail_initialize = False

    class TrackedLedger(DocumentPlanningLedger):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            ledgers.append(self)

        def initialize(self, *args, **kwargs):
            if fail_initialize:
                raise RuntimeError("synthetic initialization failure")
            return super().initialize(*args, **kwargs)

        def close(self):
            closed.append(self)
            super().close()

    monkeypatch.setattr("openkb.agent.document_orchestrator.DocumentPlanningLedger", TrackedLedger)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            mock_caller=lambda *_args, **_kwargs: pytest.fail("empty document must not call model"),
        )
    assert closed == ledgers

    fail_initialize = True
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with pytest.raises(RuntimeError, match="synthetic initialization failure"):
            plan_document(tmp_path, workspace, source, parsed, None, settings, checkpoints)
    assert closed == ledgers and len(closed) == 2


def test_corrupt_planning_ledger_is_quarantined_and_replanned(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(1)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    calls = []

    def caller(messages, **_):
        calls.append(messages)
        return {
            "overview": {"text": "Complete", "ranges": [[0, 1]], "limitations": []},
            "page_changes": [
                {
                    "local_key": "page",
                    "target_key": "",
                    "kind": "concept",
                    "name": "concepts/recovered",
                    "title": "Recovered",
                    "purpose": "The accepted result is rebuilt from its immutable request.",
                    "subject_ranges": [[0, 1]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        first = plan_document(
            tmp_path, workspace, source, parsed, None, settings, checkpoints, mock_caller=caller
        )
        ledger = next((checkpoints.root / "planning-ledgers").glob("*.sqlite3"))
        ledger.write_bytes(b"not a sqlite database")
        resumed = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            resume=True,
            mock_caller=caller,
        )

    assert first.metadata["status"] == resumed.metadata["status"] == "accepted"
    assert len(calls) == 2
    assert list((ledger.parent / "quarantine").glob("*.sqlite3"))


def test_malformed_planning_ledger_metadata_is_quarantined_and_replanned(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(1)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    calls = []

    def caller(_messages, **_):
        calls.append(None)
        return {
            "overview": {"text": "Complete", "ranges": [[0, 1]], "limitations": []},
            "page_changes": [
                {
                    "local_key": "page",
                    "target_key": "",
                    "kind": "concept",
                    "name": "concepts/recovered",
                    "title": "Recovered",
                    "purpose": "The accepted result is rebuilt from immutable inputs.",
                    "subject_ranges": [[0, 1]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        plan_document(
            tmp_path, workspace, source, parsed, None, settings, checkpoints, mock_caller=caller
        )
        ledger = next((checkpoints.root / "planning-ledgers").glob("*.sqlite3"))
        with sqlite3.connect(ledger) as db:
            db.execute("UPDATE meta SET value = '{' WHERE name = 'windows'")
        resumed = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            resume=True,
            mock_caller=caller,
        )

    assert resumed.metadata["status"] == "accepted"
    assert len(calls) == 2
    assert list((ledger.parent / "quarantine").glob("*.sqlite3"))


def test_nonresume_planning_rebuilds_an_existing_ledger(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(1)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    calls = []

    def caller(_messages, **_):
        calls.append(None)
        return {
            "overview": {"text": "Complete", "ranges": [[0, 1]], "limitations": []},
            "page_changes": [
                {
                    "local_key": "page",
                    "target_key": "",
                    "kind": "concept",
                    "name": "concepts/fresh",
                    "title": "Fresh",
                    "purpose": "A non-resume request starts from a fresh planning ledger.",
                    "subject_ranges": [[0, 1]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        first = plan_document(
            tmp_path, workspace, source, parsed, None, settings, checkpoints, mock_caller=caller
        )
        second = plan_document(
            tmp_path, workspace, source, parsed, None, settings, checkpoints, mock_caller=caller
        )

    assert first.metadata["status"] == second.metadata["status"] == "accepted"
    assert len(calls) == 2


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


def test_plan_document_uses_a_full_fallback_for_an_empty_navigation_manifest(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(2)
    navigation = {
        "id": "nav-degraded",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "basic",
        "windows": [],
        "nodes": [],
    }
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    targets = []

    def caller(messages, **_):
        target = json.loads(messages[-1]["content"])["target"]
        targets.append([target["target_start"], target["target_end"]])
        return {
            "overview": {"text": "Fallback coverage", "ranges": [[0, 2]], "limitations": []},
            "page_changes": [],
            "source_only": [{"ranges": [[0, 2]], "reason": "Kept with the source."}],
            "unresolved": [],
            "resolutions": [],
        }

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
        )

    assert targets == [[0, 2]]
    assert plan.metadata["status"] == "accepted"


def test_single_window_planner_receives_late_navigation_summaries_when_they_fit(tmp_path):
    source, parsed = _DummySource(), _DummyParsed(26)
    navigation = {
        "id": "nav-full-target",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "complete",
        "windows": [
            {
                "evidence": evidence_descriptor(source, parsed, 0, 26),
                "target_start": 0,
                "target_end": 26,
                "status": "complete",
                "reason": "",
                "target_tokens": 1000,
            }
        ],
        "nodes": [
            {"start": index, "title": f"Section {index}", "summary": f"Summary {index}"}
            for index in range(26)
        ],
    }
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    seen_hints = []

    def caller(messages, **_):
        payload = json.loads(messages[-1]["content"])
        seen_hints.extend(payload["navigation"]["hints"])
        return {
            "overview": {"text": "Complete target", "ranges": [[0, 26]], "limitations": []},
            "page_changes": [],
            "source_only": [{"ranges": [[0, 26]], "reason": "Retained with source."}],
            "unresolved": [],
            "resolutions": [],
        }

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
        )

    assert [hint["title"] for hint in seen_hints] == [f"Section {i}" for i in range(26)]


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


def test_multi_window_accumulation_and_resolution(tmp_path, monkeypatch):
    source = _DummySource()
    parsed = _DummyParsed(6)
    windows = _make_mock_windows(parsed)
    navigation = {
        "id": "nav_1",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "complete",
        "windows": windows,
        "nodes": [],
    }

    settings = {
        "model": "mock-model",
        "processing": OFFLINE_PROCESSING,
    }

    call_count = 0

    def mock_llm_call(msgs, **kwargs):
        nonlocal call_count
        call_count += 1
        user_body = json.loads(msgs[-1]["content"])
        target = user_body["target"]
        assert target["total_blocks"] == 6

        if target["target_start"] == 0:
            # Window 1: returns overview (partial), page p1 with unresolved blocking dep
            return {
                "overview": {
                    "text": "Overview of first half: system introduction.",
                    "ranges": [[0, 3]],
                    "limitations": ["Requires setup procedure from second half"],
                },
                "page_changes": [
                    {
                        "local_key": "c1",
                        "target_key": "",
                        "kind": "concept",
                        "name": "concepts/core-system",
                        "title": "Core System",
                        "purpose": "Core system components",
                        "subject_ranges": [[0, 2]],
                        "necessary_context": [],
                    }
                ],
                "source_only": [
                    {
                        "ranges": [[2, 3]],
                        "reason": "Introductory notes",
                    }
                ],
                "unresolved": [
                    {
                        "location": [[1, 2]],
                        "problem_type": "missing_prerequisite",
                        "missing_target": "setup-guide",
                        "affected_pages": ["c1"],
                        "blocking": True,
                        "reason": "Setup guide needed before core system can be deployed",
                    }
                ],
                "resolutions": [],
            }
        else:
            # Window 2: returns overview (complete), page p2 (setup guide), extends p1, resolves u1!
            carry = user_body["carry"]
            assert len(carry["page_register"]) == 1
            assert len(carry["open_references"]) == 1

            return {
                "overview": {
                    "text": "Complete overview of the entire system and deployment.",
                    "ranges": [[0, 6]],
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "c2",
                        "target_key": "p1",  # Extending existing p1
                        "kind": "concept",
                        "name": "concepts/core-system",
                        "title": "Core System",
                        "purpose": "Core system components and operations",
                        "subject_ranges": [[3, 4]],
                        "necessary_context": [],
                    },
                    {
                        "local_key": "c3",
                        "target_key": "",  # New page p2
                        "kind": "concept",
                        "name": "concepts/setup-guide",
                        "title": "Setup Guide",
                        "purpose": "Installation and initial setup",
                        "subject_ranges": [[4, 6]],
                        "necessary_context": [
                            {
                                "relation": "explicit_reference",
                                "ranges": [[4, 5]],
                                "basis": "Content of block 4.",
                                "basis_ranges": [[4, 5]],
                            }
                        ],
                    },
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [
                    {
                        "unresolved_key": carry["open_references"][0]["key"],
                        "basis_ranges": [[4, 5]],
                    }
                ],
            }

    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True, exist_ok=True)

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=mock_llm_call,
            resume=False,
        )

        assert call_count == 2
        assert plan.overview.status == "complete"
        assert "Complete overview" in plan.overview.text
        assert len(plan.pages) == 2

        # A resolved dependency remains separate formal-plan evidence, not a
        # model-authored necessary-context relation or newly classified body.
        p1 = plan.pages[0]
        assert p1.key == "p1"
        assert p1.name == "concepts/core-system"
        assert p1.subject_ranges == [[0, 2], [3, 4]]
        assert p1.necessary_context == []
        # And since u1 was resolved, p1 should be ready, NOT blocked!
        assert p1.state == "ready"

        # Page 2 (setup guide)
        p2 = plan.pages[1]
        assert p2.key == "p2"
        assert p2.name == "concepts/setup-guide"
        assert p2.subject_ranges == [[4, 6]]
        assert p2.necessary_context[0]["basis"] == "Content of block 4. "
        assert p2.state == "ready"

        # Check resolution
        assert len(plan.resolutions) == 1
        assert plan.resolutions[0].basis_ranges == [[4, 5]]
        assert plan.unresolved[0].status == "resolved"
        from openkb.agent.document_page_evidence import (
            page_occurrence_descriptors,
            page_resolution_ranges,
        )

        resolution_ranges = page_resolution_ranges(plan, p1)
        assert resolution_ranges == [[4, 5]]
        resolution_occurrences = page_occurrence_descriptors(
            p1,
            source,
            parsed,
            resolution_ranges=resolution_ranges,
        )
        assert any(
            route["route"] == "resolution_evidence"
            for occurrence in resolution_occurrences
            for route in occurrence["routes"]
        )


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


def test_plan_document_idempotent_resume(tmp_path, monkeypatch):
    source = _DummySource()
    parsed = _DummyParsed(3)
    # Single window 0..3
    windows = [
        {
            "evidence": evidence_descriptor(source, parsed, 0, 3),
            "target_start": 0,
            "target_end": 3,
            "status": "complete",
            "reason": "",
            "target_tokens": 1000,
        }
    ]
    navigation = {
        "id": "nav_single",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "complete",
        "windows": windows,
        "nodes": [],
    }
    settings = {
        "model": "mock-model",
        "processing": OFFLINE_PROCESSING,
    }

    call_count = 0

    def mock_llm_call(msgs, **kwargs):
        nonlocal call_count
        call_count += 1
        return {
            "overview": {"text": "Single window overview", "ranges": [[0, 3]], "limitations": []},
            "page_changes": [
                {
                    "local_key": "c1",
                    "target_key": "",
                    "kind": "concept",
                    "name": "concepts/single",
                    "title": "Single Topic",
                    "purpose": "Single topic purpose",
                    "subject_ranges": [[0, 3]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True, exist_ok=True)

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        # First run
        plan1 = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=mock_llm_call,
            resume=False,
        )
        assert call_count == 1
        assert len(plan1.pages) == 1

        # Second run with resume=True -> should use cached recovery plan without LLM call!
        plan2 = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=mock_llm_call,
            resume=True,
        )
        assert call_count == 1  # No additional LLM calls!
        assert plan2.metadata == plan1.metadata
        assert len(plan2.pages) == 1


def test_plan_document_replans_after_invalid_recovered_plan(tmp_path, monkeypatch):
    source = _DummySource()
    parsed = _DummyParsed(3)
    navigation = {
        "id": "nav_recovery_validation",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "complete",
        "windows": [
            {
                "evidence": evidence_descriptor(source, parsed, 0, 3),
                "target_start": 0,
                "target_end": 3,
                "status": "complete",
                "reason": "",
                "target_tokens": 1000,
            }
        ],
        "nodes": [],
    }
    settings = {
        "model": "mock-model",
        "processing": OFFLINE_PROCESSING,
    }
    calls = 0

    def caller(*_, **__):
        nonlocal calls
        calls += 1
        return {
            "overview": {"text": "Recovered safely", "ranges": [[0, 3]], "limitations": []},
            "page_changes": [
                {
                    "local_key": "recovered",
                    "target_key": "",
                    "kind": "concept",
                    "name": "concepts/recovered-safely",
                    "title": "Recovered Safely",
                    "purpose": "Demonstrate safe recovery validation",
                    "subject_ranges": [[0, 3]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        first = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
            resume=False,
        )
        invalid = to_dict(first)
        invalid["pages"][0]["kind"] = "not-a-page-kind"
        original_load_recovery = checkpoints.load_recovery

        def load_invalid_plan(key, kind):
            return invalid if kind == "plan" else original_load_recovery(key, kind)

        monkeypatch.setattr(checkpoints, "load_recovery", load_invalid_plan)
        replanned = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
            resume=True,
        )
        invalid = to_dict(first)
        invalid["metadata"]["status"] = "pending"
        invalid["pages"][0]["subject_ranges"] = []
        coverage_replanned = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
            resume=True,
        )
        invalid = to_dict(first)
        invalid["metadata"]["status"] = "pending"
        terminal_resumed = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
            resume=True,
        )

    assert calls == 3
    assert replanned.pages[0].kind == "concept"
    assert coverage_replanned.pages[0].subject_ranges == [[0, 3]]
    assert terminal_resumed.metadata["status"] == "accepted"


def test_plan_document_resumes_from_first_unaccepted_window(tmp_path):
    source = _DummySource()
    parsed = _DummyParsed(6)
    navigation = {
        "id": "nav_resume_partial",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "complete",
        "windows": _make_mock_windows(parsed),
        "nodes": [],
    }
    settings = {
        "model": "mock-model",
        "processing": OFFLINE_PROCESSING,
    }
    calls = []
    fail_second_window = True

    def caller(messages, **_):
        nonlocal fail_second_window
        target = json.loads(messages[-1]["content"])["target"]
        calls.append(target["target_start"])
        if target["target_start"] == 3 and fail_second_window:
            raise ProcessingIncomplete("document_response_invalid", "planning")
        suffix = "first" if target["target_start"] == 0 else "second"
        return {
            "overview": {
                "text": f"{suffix} accepted",
                "ranges": [[target["target_start"], target["target_end"]]],
                "limitations": [],
            },
            "page_changes": [
                {
                    "local_key": suffix,
                    "target_key": "",
                    "kind": "concept",
                    "name": f"concepts/{suffix}",
                    "title": suffix.title(),
                    "purpose": suffix,
                    "subject_ranges": [[target["target_start"], target["target_end"]]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with pytest.raises(ProcessingIncomplete):
            plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                navigation,
                settings,
                checkpoints,
                mock_caller=caller,
            )
        progress_path = next((checkpoints.root / "recovery").glob("*-plan.json"))
        progress_record = json.loads(progress_path.read_text())
        preview = progress_record["value"]["preview"]
        assert preview["overview"]["text"] == "first accepted"
        assert [page["name"] for page in preview["pages"]] == ["concepts/first"]
        assert "blocks" not in progress_record["value"]
        artifacts = artifact_summaries(
            checkpoints.store,
            source.source_id,
            source.id,
            parsed.id,
            stages=("planning",),
        )
        plan_summary = next(
            row
            for row in artifacts or ()
            if row["storage"] == "plan" and row["key"] == progress_record["key"]
        )
        assert plan_summary["progress"]["counts"]["pages"] == 1
        fail_second_window = False
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
            resume=True,
        )

    assert calls == [0, 3, 3]
    assert [page.name for page in plan.pages] == ["concepts/first", "concepts/second"]
    assert plan.overview.status == "complete"


def test_resume_rebuilds_an_unaccepted_ledger_against_a_new_catalogue_page(tmp_path):
    """A pre-W interruption cannot make a later page appear unreserved."""

    source, parsed = _DummySource(), _DummyParsed(1)
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    workspace = tmp_path / "workspace"
    (workspace / "wiki" / "concepts").mkdir(parents=True)
    visible_catalogues = []

    def caller(messages, **_):
        request = json.loads(messages[-1]["content"])
        visible = list(request["existing_targets"])
        visible_catalogues.append(visible)
        if len(visible_catalogues) == 1:
            raise ProcessingIncomplete("interrupted_for_resume", "planning")
        target = "concepts/existing" if "concepts/existing" in visible else ""
        return {
            "overview": {
                "text": "Plan the source against the current catalogue.",
                "ranges": [[0, 1]],
                "limitations": [],
            },
            "page_changes": [
                {
                    "local_key": "existing",
                    "target_key": "",
                    "target": target,
                    "kind": "concept",
                    "name": "concepts/existing",
                    "title": "Existing",
                    "purpose": "Extend the page created while planning was paused.",
                    "subject_ranges": [[0, 1]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with pytest.raises(ProcessingIncomplete, match="interrupted_for_resume"):
            plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                None,
                settings,
                checkpoints,
                mock_caller=caller,
            )
        (workspace / "wiki" / "concepts" / "existing.md").write_text(
            "# Existing\nCreated while planning was paused.\n", encoding="utf-8"
        )
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            None,
            settings,
            checkpoints,
            mock_caller=caller,
            resume=True,
        )

    assert visible_catalogues == [[], ["concepts/existing"]]
    assert plan.metadata["catalog_targets"] == ["concepts/existing"]
    assert [(page.name, page.target) for page in plan.pages] == [
        ("concepts/existing", "concepts/existing")
    ]


def test_resume_resets_a_structurally_invalid_json_ledger_row(tmp_path, monkeypatch):
    """A fresh digest cannot make an invalid pending ledger row resumable."""

    from openkb.agent import document_orchestrator

    source, parsed = _DummySource(), _DummyParsed(6)
    navigation = {
        "id": "nav-invalid-row",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "complete",
        "windows": _make_mock_windows(parsed),
        "nodes": [],
    }
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    original_ledger = document_orchestrator.DocumentPlanningLedger
    ledgers = []

    def tracking_ledger(*args, **kwargs):
        ledger = original_ledger(*args, **kwargs)
        ledgers.append(ledger)
        return ledger

    calls, interrupt_second = [], True

    def caller(messages, **_):
        nonlocal interrupt_second
        target = json.loads(messages[-1]["content"])["target"]
        start, end = target["target_start"], target["target_end"]
        calls.append(start)
        if start == 3 and interrupt_second:
            raise ProcessingIncomplete("interrupted_for_resume", "planning")
        return {
            "overview": {"text": "Recovered plan.", "ranges": [[start, end]], "limitations": []},
            "page_changes": [
                {
                    "local_key": f"part-{start}",
                    "target_key": "",
                    "kind": "concept",
                    "name": f"concepts/part-{start}",
                    "title": f"Part {start}",
                    "purpose": "A recovered planning page.",
                    "subject_ranges": [[start, end]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(document_orchestrator, "DocumentPlanningLedger", tracking_ledger)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with pytest.raises(ProcessingIncomplete, match="interrupted_for_resume"):
            plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                navigation,
                settings,
                checkpoints,
                mock_caller=caller,
            )
        # The payload stays valid JSON and its state digest is recomputed, so
        # only durable-row schema validation can distinguish this from a safe
        # accepted prefix.
        tampered = original_ledger(checkpoints, ledgers[0].recovery_key)
        try:
            with tampered._transaction():
                tampered.db.execute("UPDATE pages SET payload = ?", ('{"bad":"row"}',))
                tampered._refresh_integrity()
        finally:
            tampered.close()
        interrupt_second = False
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
            resume=True,
        )

    assert calls == [0, 3, 0, 3]
    assert [page.name for page in plan.pages] == ["concepts/part-0", "concepts/part-3"]


def test_output_pressure_splits_only_target_and_resumes_with_the_same_frozen_evidence(tmp_path):
    source = _DummySource()
    parsed = _DummyParsed(2)
    descriptor = evidence_descriptor(source, parsed, 0, 2)
    navigation = {
        "id": "nav_output_pressure",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "complete",
        "windows": [
            {
                "evidence": descriptor,
                "target_start": 0,
                "target_end": 2,
                "status": "complete",
                "reason": "",
                "target_tokens": 1000,
            }
        ],
        "nodes": [],
    }
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    calls, frozen_payloads = [], []
    stop_first_split_target = True

    def caller(messages, **_):
        nonlocal stop_first_split_target
        request = json.loads(messages[-1]["content"])
        target = request["target"]
        calls.append(target)
        if "ranges" not in target:
            raise ProcessingIncomplete("output_budget_exhausted", "planning")
        frozen_payloads.append(request["evidence"])
        index = target["ranges"][0]["block_index"]
        if index == 0 and stop_first_split_target:
            raise ProcessingIncomplete("interrupted_for_resume", "planning")
        return {
            "overview": {
                "text": "Split target plan",
                "ranges": target["ranges"] if index == 0 else [[0, 2]],
                "limitations": [],
            },
            "page_changes": [
                {
                    "local_key": f"part-{index}",
                    "target_key": "",
                    "kind": "concept",
                    "name": f"concepts/part-{index}",
                    "title": f"Part {index}",
                    "purpose": "Output-pressure target split",
                    "subject_ranges": target["ranges"],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with pytest.raises(ProcessingIncomplete, match="interrupted_for_resume"):
            plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                navigation,
                settings,
                checkpoints,
                mock_caller=caller,
            )
        stop_first_split_target = False
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
            resume=True,
        )

    assert "ranges" not in calls[0]
    assert [target["ranges"][0]["block_index"] for target in calls[1:]] == [0, 0, 1]
    assert frozen_payloads[0] == frozen_payloads[1] == frozen_payloads[2]
    receipts = plan.metadata["accepted_window_receipts"]
    assert [item["target_ranges"][0]["block_index"] for item in receipts] == [0, 1]
    assert all(item["checkpoint"] is None and len(item["result"]) == 64 for item in receipts)


def test_reload_schedule_is_durable_before_its_first_child_is_accepted(tmp_path, monkeypatch):
    """A capacity reload resumes at child W/T rather than re-sending its parent."""

    from openkb.agent import document_windowing

    source = _DummySource()
    parsed = _DummyParsed(2)
    navigation = {
        "id": "nav-reload-recovery",
        "source_id": source.source_id,
        "version_id": source.id,
        "parse_id": parsed.id,
        "status": "complete",
        "windows": [
            {
                "evidence": evidence_descriptor(source, parsed, 0, 2),
                "target_start": 0,
                "target_end": 2,
                "status": "complete",
                "reason": "",
                "target_tokens": 1_000,
            }
        ],
        "nodes": [],
    }
    settings = {"model": "mock-model", "processing": OFFLINE_PROCESSING}
    original_project = document_windowing.project_planning_view
    reload_parent = True
    calls = []

    def project(*args, **kwargs):
        nonlocal reload_parent
        if reload_parent:
            reload_parent = False
            raise ProcessingIncomplete("planning_context_exceeds_request_budget", "planning")
        return original_project(*args, **kwargs)

    def caller(messages, **_):
        target = json.loads(messages[-1]["content"])["target"]
        calls.append((target["target_start"], target["target_end"]))
        if len(calls) == 1:
            raise ProcessingIncomplete("interrupted_for_resume", "planning")
        return {
            "overview": {
                "text": "Reloaded child plan.",
                "ranges": [[target["target_start"], target["target_end"]]],
                "limitations": [],
            },
            "page_changes": [
                {
                    "local_key": f"part-{target['target_start']}",
                    "target_key": "",
                    "kind": "concept",
                    "name": f"concepts/part-{target['target_start']}",
                    "title": f"Part {target['target_start']}",
                    "purpose": "Reloaded planning child.",
                    "subject_ranges": [[target["target_start"], target["target_end"]]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    monkeypatch.setattr(document_windowing, "project_planning_view", project)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        with pytest.raises(ProcessingIncomplete, match="interrupted_for_resume"):
            plan_document(
                tmp_path,
                workspace,
                source,
                parsed,
                navigation,
                settings,
                checkpoints,
                mock_caller=caller,
            )
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
            resume=True,
        )

    assert calls == [(0, 1), (0, 1), (1, 2)]
    assert [page.name for page in plan.pages] == ["concepts/part-0", "concepts/part-1"]


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


def test_hidden_registered_page_is_promoted_before_retrying_a_stable_key(tmp_path, monkeypatch):
    import litellm

    source, parsed = _DummySource(), _DummyParsed(2)
    navigation = _two_window_navigation(source, parsed, "nav-projection-collision")
    settings = _crowded_planning_settings()
    monkeypatch.setattr(litellm, "token_counter", _crowded_planning_counter)
    carries = []

    def caller(messages, **_):
        request = json.loads(messages[-1]["content"])
        target = request["target"]
        if target["target_start"] == 0:
            return {
                "overview": {"text": "First", "ranges": [[0, 1]], "limitations": []},
                "page_changes": _crowded_page_changes(100, [[0, 1]]),
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        carries.append({row["key"] for row in request["carry"]["page_register"]})
        return {
            "overview": {"text": "Complete", "ranges": [[0, 2]], "limitations": []},
            "page_changes": [
                {
                    "local_key": "extend-prior",
                    "target_key": "p100",
                    "kind": "concept",
                    "name": "concepts/prior-100",
                    "title": "Prior 100",
                    "purpose": "The final historical page now gains current evidence.",
                    "subject_ranges": [[1, 2]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
        )

    assert "p100" not in carries[0]
    assert "p100" in carries[1]
    assert plan.pages[-1].subject_ranges == [[0, 1], [1, 2]]


def test_later_named_prerequisite_promotes_oversized_ledger_before_resolution(
    tmp_path, monkeypatch
):
    import litellm

    source, parsed = _DummySource(), _DummyParsed(2)
    parsed.blocks[1].text = "Setup guide: configure the required service first."
    navigation = _two_window_navigation(source, parsed, "nav-prerequisite-projection")
    settings = _crowded_planning_settings()
    monkeypatch.setattr(litellm, "token_counter", _crowded_planning_counter)
    carries = []

    def caller(messages, **_):
        request = json.loads(messages[-1]["content"])
        target = request["target"]
        if target["target_start"] == 0:
            return {
                "overview": {"text": "First", "ranges": [[0, 1]], "limitations": []},
                "page_changes": _crowded_page_changes(100, [[0, 1]]),
                "source_only": [],
                "unresolved": [
                    {
                        "location": [[0, 1]],
                        "problem_type": "missing_prerequisite",
                        "missing_target": "setup guide",
                        "affected_pages": ["p100"],
                        "blocking": True,
                        "reason": "The setup guide is needed before this page is applicable.",
                    }
                ],
                "resolutions": [],
            }
        carries.append(request["carry"])
        page_keys = {row["key"] for row in request["carry"]["page_register"]}
        assert "p100" in page_keys
        assert len(request["carry"]["open_references"]) == 1
        unresolved_key = request["carry"]["open_references"][0]["key"]
        return {
            "overview": {"text": "Complete", "ranges": [[0, 2]], "limitations": []},
            "page_changes": [
                {
                    "local_key": "extend-prior",
                    "target_key": "p100",
                    "kind": "concept",
                    "name": "concepts/prior-100",
                    "title": "Prior 100",
                    "purpose": "The prerequisite is now established.",
                    "subject_ranges": [[1, 2]],
                    "necessary_context": [],
                }
            ],
            "source_only": [],
            "unresolved": [],
            "resolutions": [{"unresolved_key": unresolved_key, "basis_ranges": [[1, 2]]}],
        }

    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
        )

    assert len(carries) == 1
    assert plan.pages[-1].state == "ready"
    assert plan.unresolved[0].status == "resolved"


def test_hidden_open_unresolved_is_promoted_before_a_stable_resolution(tmp_path, monkeypatch):
    import litellm

    source, parsed = _DummySource(), _DummyParsed(2)
    navigation = _two_window_navigation(source, parsed, "nav-resolution-projection")
    settings = _crowded_planning_settings()
    monkeypatch.setattr(litellm, "token_counter", _crowded_planning_counter)
    carries = []

    def caller(messages, **_):
        request = json.loads(messages[-1]["content"])
        target = request["target"]
        if target["target_start"] == 0:
            return {
                "overview": {"text": "First", "ranges": [[0, 1]], "limitations": []},
                "page_changes": _crowded_page_changes(100, [[0, 1]]),
                "source_only": [],
                "unresolved": [
                    {
                        "location": [[0, 1]],
                        "problem_type": "missing_prerequisite",
                        "missing_target": f"requirement-{index}",
                        "affected_pages": [f"p{index}"],
                        "blocking": True,
                        "reason": "The prerequisite has not yet been supplied.",
                    }
                    for index in range(1, 101)
                ],
                "resolutions": [],
            }
        carries.append({row["key"] for row in request["carry"]["open_references"]})
        return {
            "overview": {"text": "Complete", "ranges": [[0, 2]], "limitations": []},
            "page_changes": [],
            "source_only": [{"ranges": [[1, 2]], "reason": "This section remains source-only."}],
            "unresolved": [],
            "resolutions": [{"unresolved_key": "u100", "basis_ranges": [[1, 2]]}],
        }

    workspace = tmp_path / "workspace"
    (workspace / "wiki").mkdir(parents=True)
    with CompilationCheckpoints(tmp_path, source, parsed, settings, None) as checkpoints:
        plan = plan_document(
            tmp_path,
            workspace,
            source,
            parsed,
            navigation,
            settings,
            checkpoints,
            mock_caller=caller,
        )

    assert "u100" not in carries[0]
    assert "u100" in carries[1]
    assert next(item for item in plan.unresolved if item.key == "u100").status == "resolved"
