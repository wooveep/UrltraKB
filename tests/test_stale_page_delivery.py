"""A changed human page is terminal even when original-source proof also fails."""

import json

import pytest

from ultrakb.agent.page_delivery import validate_page_delivery
from ultrakb.agent.page_response import PageResponseError
from ultrakb.agent.page_updates import PageUpdate
from ultrakb.agent.source_excerpts import SourceExcerpts


def update_response(patch, content, *, description=None):
    return json.dumps(
        {
            "base_sha256": patch.digest,
            **({"description": description} if description is not None else {}),
            "edits": [{"block_id": "b1", "operation": "replace", "content": content}],
        }
    )


@pytest.mark.unit
def test_stale_page_takes_priority_over_an_unproven_original_path():
    original = "Human recovery notes.\n"
    patch = PageUpdate(original)
    raw = json.dumps(
        {
            "base_sha256": patch.digest,
            "edits": [
                {
                    "block_id": "b1",
                    "operation": "replace",
                    "content": "Read `/systen/config/state`.\n",
                }
            ],
        }
    )
    with pytest.raises(PageResponseError) as caught:
        validate_page_delivery(
            raw,
            page_update=patch,
            read_current=lambda: original + "Human addition made during generation.\n",
            original_excerpts=SourceExcerpts(["Read /system/config/state.\n"]),
        )
    assert caught.value.code == "page_update_stale"


@pytest.mark.unit
@pytest.mark.parametrize("stale", [False, True], ids=["current", "human-edited"])
@pytest.mark.parametrize(
    ("content", "description", "proof_error"),
    [
        ("Read `/systen/config/state`.\n", None, None),
        ("[[original:x9.9]]\n", None, "page_excerpt_invalid"),
        ("Read `/system/config/state`.\n", "Read `/missing/state`.", None),
    ],
    ids=["unproven-path", "unknown-excerpt", "unproven-metadata"],
)
def test_stale_classification_is_stable_across_original_proof_errors(
    stale, content, description, proof_error
):
    original = "Human recovery notes.\n"
    patch = PageUpdate(original)
    current_reads = []

    def read_current():
        current_reads.append(True)
        return original + "Human addition.\n" if stale else original

    def deliver():
        return validate_page_delivery(
            update_response(patch, content, description=description),
            page_update=patch,
            read_current=read_current,
            original_excerpts=SourceExcerpts(["Read /system/config/state.\n"]),
        )

    if stale or proof_error:
        with pytest.raises(PageResponseError) as caught:
            deliver()
        assert caught.value.code == ("page_update_stale" if stale else proof_error)
    else:
        assert content in json.loads(deliver())["content"]
    assert len(current_reads) == 1


@pytest.mark.unit
@pytest.mark.parametrize("valid_proof", [False, True], ids=["invalid-proof", "valid-proof"])
def test_human_edit_during_original_validation_is_terminal(monkeypatch, valid_proof):
    original = "Human recovery notes.\n"
    current = [original]
    patch = PageUpdate(original)
    excerpts = SourceExcerpts(["Read /system/config/state.\n"])
    validate_original = excerpts.response

    def receive_original_proof(*args, **kwargs):
        # Schedule a human edit at the validation boundary. The actual proof
        # validator and its result/error are preserved; no LLM reply is replaced.
        try:
            return validate_original(*args, **kwargs)
        finally:
            current[0] += "Human addition during original validation.\n"

    monkeypatch.setattr(excerpts, "response", receive_original_proof)
    content = "[[original:x1.1]]" if valid_proof else "[[original:x9.9]]"
    with pytest.raises(PageResponseError) as caught:
        validate_page_delivery(
            update_response(patch, content),
            page_update=patch,
            read_current=lambda: current[0],
            original_excerpts=excerpts,
        )
    assert caught.value.code == "page_update_stale"


@pytest.mark.unit
def test_source_proof_does_not_allow_removing_a_preserved_human_annotation():
    annotation = "Human annotation: verify the maintenance window."
    original = annotation + "\n"
    patch = PageUpdate(original, preserved_fragments=(annotation,))
    with pytest.raises(PageResponseError) as caught:
        validate_page_delivery(
            update_response(patch, "Read `/system/config/state`.\n"),
            page_update=patch,
            read_current=lambda: original,
            original_excerpts=SourceExcerpts(["Read /system/config/state.\n"]),
        )
    assert caught.value.code == "page_manual_content_removed"
