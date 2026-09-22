"""Range-aware generation helpers keep required context with every fragment."""

import threading
import time

import pytest

from openkb.agent.document_pages import _estimated_tokens, _page_assets, _split_occurrences
from openkb.agent.shared_resources import SharedResourcePool
from openkb.processing import RequestLimits


def _occurrence(identifier, route, text="A complete source statement.", location=None):
    return {
        "id": identifier,
        "routes": [{"route": route}],
        "reference": {
            "source_id": "src_1",
            "version_id": "ver_1",
            "parse_id": "parse_1",
            "block_id": identifier,
            "start": 0,
            "end": len(text),
        },
        "text": text,
        "location": location or {},
    }


def test_split_repeats_necessary_context_for_each_body_partition():
    first = _occurrence("body-1", "page_body")
    context = _occurrence("condition", "context_only", "Apply only when enabled.")
    second = _occurrence("body-2", "page_body")

    parts = _split_occurrences([first, context, second])

    assert [[item["id"] for item in part] for part in parts] == [
        ["body-1", "condition"],
        ["condition", "body-2"],
    ]


def test_split_single_body_keeps_necessary_context_with_both_text_fragments():
    body = _occurrence("body", "page_body", "First paragraph.\nSecond paragraph.")
    context = _occurrence("condition", "context_only", "Apply only when enabled.")

    parts = _split_occurrences([body, context])

    assert len(parts) == 2
    assert [[item["id"] for item in part] for part in parts] == [
        ["bodya", "condition"],
        ["bodyb", "condition"],
    ]


def test_split_keeps_each_native_table_row_in_one_generation_batch():
    row_one = [
        _occurrence(
            f"row-1-cell-{cell}",
            "page_body",
            location={"kind": "docx", "table": 1, "row": 1, "cell": cell},
        )
        for cell in (1, 2)
    ]
    row_two = [
        _occurrence(
            f"row-2-cell-{cell}",
            "page_body",
            location={"kind": "docx", "table": 1, "row": 2, "cell": cell},
        )
        for cell in (1, 2)
    ]

    parts = _split_occurrences([*row_one, *row_two])

    assert [[item["id"] for item in part] for part in parts] == [
        ["row-1-cell-1", "row-1-cell-2"],
        ["row-2-cell-1", "row-2-cell-2"],
    ]


def test_split_refuses_to_character_fragment_a_single_oversized_table_row():
    row = _occurrence(
        "row-1-cell-1",
        "page_body",
        "A deliberately oversized native table cell.",
        location={"kind": "docx", "table": 1, "row": 1, "cell": 1},
    )

    # The generation retry path treats this empty result as the explicit
    # planned_page_evidence_exceeds_request_budget omission.  It must not
    # replace the native row with bodya/bodyb character fragments.
    assert _split_occurrences([row]) == []


def test_page_assets_excludes_unread_source_images_but_keeps_bound_figure_derivations():
    original, frame, ocr, unrelated = (character * 64 for character in "abcz")
    occurrences = [
        {
            "assets": [original],
            "context_data": {
                "image_relations": [
                    {
                        "original_asset": original,
                        "frames": [{"asset": frame, "ocr_assets": [ocr]}],
                    }
                ]
            },
        }
    ]
    available = {
        original: "../sources/images/original.png",
        frame: "../sources/images/frame.png",
        ocr: "../sources/images/ocr.png",
        unrelated: "../sources/images/unrelated.png",
    }

    assert _page_assets(occurrences, available) == {
        original: "../sources/images/original.png",
        frame: "../sources/images/frame.png",
        ocr: "../sources/images/ocr.png",
    }


def test_published_page_recovery_rejects_a_forged_critical_review_receipt():
    """A receipt-shaped dict is not proof without its immutable checkpoint."""

    from openkb.agent.document_page_contracts import restore_published_document_page_candidate
    from openkb.agent.document_plan import PagePlan
    from openkb.sources import content_id

    content = "# Candidate\n"
    digest = content_id(content)
    receipt = {
        "mode": "critical",
        "candidate": digest,
        "candidate_recovery": "a" * 64,
        "publication_identity": "b" * 64,
        "verdict": "supported",
        "reviews": [
            {
                "verdict": "supported",
                "reason": "The candidate is supported.",
                "issues": [],
                "candidate": digest,
                "checkpoint": "c" * 64,
                "result": "d" * 64,
                "cached": False,
                "dispatch_output_tokens": 128,
            }
        ],
    }
    page = PagePlan(
        key="candidate",
        kind="concept",
        name="concepts/candidate",
        title="Candidate",
        purpose="Explain the candidate.",
        subject_ranges=[[0, 1]],
        quality="published",
        review_receipt=receipt,
    )

    class Checkpoints:
        def load_recovery(self, key, kind):
            assert (key, kind) == (receipt["candidate_recovery"], "draft")
            return {
                "output": {
                    "content": content,
                    "page_key": page.key,
                    "quality": "verified",
                    "candidate": digest,
                    "occurrence_ids": ["occurrence"],
                    "review_receipt": receipt,
                    "retained_identity": "retained",
                }
            }

        def record(self, key):
            assert key == receipt["reviews"][0]["checkpoint"]
            return None

    with pytest.raises(ValueError, match="review checkpoint is unavailable"):
        restore_published_document_page_candidate(Checkpoints(), page)


def test_page_pool_reserves_authoritative_input_and_current_completion_reservation(
    monkeypatch,
):
    import litellm

    limits = RequestLimits(
        context_tokens=200,
        output_tokens=10,
        request_timeout=1,
        stage_timeout=None,
        document_timeout=None,
        cleanup_timeout=1,
        max_attempts=2,
        max_requests=None,
        max_tokens=None,
        concurrency=2,
        max_context_tokens=200,
        max_output_tokens=100,
    )

    def token_counter(*, messages=None, text=None, **_):
        # The CJK / structured envelope is much more expensive than the old
        # ``len(content) // 3`` heuristic, while the format schema also costs
        # input capacity on the wire.
        if messages is not None:
            assert messages[0]["content"] == "配置" * 8
            return 180
        assert text is not None
        return 20

    monkeypatch.setattr(litellm, "token_counter", token_counter)
    reservation = _estimated_tokens([{"content": "配置" * 8}], limits, "mock-model")
    assert reservation == 210  # 200 exact input + 10 current output reservation.
    pool = SharedResourcePool(2, 400)
    first_entered, release_first, second_entered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def hold_first():
        with pool.admit(reservation):
            first_entered.set()
            assert release_first.wait(1)

    def enter_second():
        with pool.admit(reservation):
            second_entered.set()

    first = threading.Thread(target=hold_first)
    second = threading.Thread(target=enter_second)
    first.start()
    assert first_entered.wait(1)
    second.start()
    assert not second_entered.wait(0.1)
    release_first.set()
    first.join(1)
    second.join(1)
    assert second_entered.is_set()


@pytest.mark.parametrize("stop_kind", ["timeout", "cancelled"])
def test_pool_waiter_stops_before_late_transport_dispatch(stop_kind):
    """A queued page request cannot become a new provider call after stopping."""

    from openkb.cancellation import OperationCancelled, cancellation_scope
    from openkb.processing import ExecutionBudget, ProcessingIncomplete, request_admission_scope

    pool = SharedResourcePool(1)
    holder_entered, release_holder, dispatched = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def hold_capacity():
        with pool.admit():
            holder_entered.set()
            assert release_holder.wait(1)

    holder = threading.Thread(target=hold_capacity)
    holder.start()
    assert holder_entered.wait(1)
    stopped = threading.Event()
    timer = None
    if stop_kind == "cancelled":
        timer = threading.Timer(0.05, stopped.set)
        timer.start()
    budget = ExecutionBudget(
        RequestLimits(
            context_tokens=1000,
            output_tokens=10,
            request_timeout=0.05 if stop_kind == "timeout" else 1,
            stage_timeout=None,
            document_timeout=None,
            cleanup_timeout=1,
            max_attempts=1,
            max_requests=None,
            max_tokens=None,
            concurrency=1,
            max_context_tokens=1000,
            max_output_tokens=10,
        )
    )
    expected = ProcessingIncomplete if stop_kind == "timeout" else OperationCancelled
    try:
        with (
            cancellation_scope(stopped.is_set),
            request_admission_scope(lambda _options, checkpoint: pool.admit(checkpoint=checkpoint)),
            pytest.raises(expected),
        ):
            budget.call(
                lambda **_options: dispatched.set(),
                model="openai/test",
                messages=[{"role": "user", "content": "JSON"}],
            )
    finally:
        if timer is not None:
            timer.cancel()
        release_holder.set()
        holder.join(1)
    # Let a buggy queued worker observe capacity becoming available.  It must
    # see the request stop latch first and never cross the function boundary.
    time.sleep(0.1)
    assert not dispatched.is_set()
