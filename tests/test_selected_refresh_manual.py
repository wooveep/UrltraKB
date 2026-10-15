"""Manual additions remain outside the source refresh's editing targets."""

import json

import pytest

from ultrakb.agent.page_updates import PageUpdate
from ultrakb.selected_refresh_manual import editable_refresh_blocks, manual_refresh_fragments


def test_inserted_review_note_is_not_an_editable_source_block():
    baseline = "---\ntype: Concept\n---\n\n# Shared\n\nThe original source rule.\n"
    current = baseline + "\nHuman review note.\n"
    patch = PageUpdate(current)
    allowed = editable_refresh_blocks(patch, baseline)
    patch.allowed_blocks = allowed
    assert all("Human review note." not in block["text"] for block in patch.request()["blocks"])
    source = next(block for block in patch.request()["blocks"] if "source rule" in block["text"])
    result = patch.apply(
        json.dumps(
            {
                "status": "updated",
                "base_sha256": patch.digest,
                "edits": [
                    {"block_id": source["id"], "operation": "replace", "content": "Updated rule.\n"}
                ],
            }
        ),
        current=current,
    )
    assert "Updated rule." in result["content"]
    assert "Human review note." in result["content"]


def test_manual_replacement_remains_reviewable_source_work():
    baseline = "---\ntype: Concept\n---\n\nOriginal rule.\n"
    current = "---\ntype: Concept\n---\n\nHuman corrected the rule.\n"
    patch = PageUpdate(current)
    assert editable_refresh_blocks(patch, baseline) == frozenset(patch.blocks)


def test_insertions_cannot_reopen_a_manually_added_target():
    baseline = "---\ntype: Concept\n---\n\nOriginal rule.\n"
    patch = PageUpdate(baseline + "\nHuman review note.\n")
    patch.allowed_blocks = editable_refresh_blocks(patch, baseline)
    protected = next(identity for identity in patch.blocks if identity not in patch.allowed_blocks)
    with pytest.raises(ValueError, match="supplied for editing"):
        patch.apply(
            json.dumps(
                {
                    "status": "updated",
                    "base_sha256": patch.digest,
                    "edits": [{"block_id": protected, "operation": "delete"}],
                }
            ),
            current=patch.original,
        )


def test_note_after_a_single_newline_survives_edits_to_its_source_paragraph():
    baseline = "---\ntype: Concept\n---\n\nThe original rule."
    current = baseline + "\nHuman review note.\n"
    patch = PageUpdate(current)
    fragments = manual_refresh_fragments(patch, baseline)
    assert fragments == ("Human review note.",)
    patch.preserved_fragments = fragments
    assert editable_refresh_blocks(patch, baseline) == frozenset(patch.blocks)
    response = {
        "status": "updated",
        "base_sha256": patch.digest,
        "edits": [{"block_id": "b1", "operation": "replace", "content": "Updated rule.\n"}],
    }
    with pytest.raises(ValueError, match="manual additions"):
        patch.apply(json.dumps(response), current=current)
    response["edits"][0]["content"] += "Human review note.\n"
    result = patch.apply(json.dumps(response), current=current)
    assert "Updated rule." in result["content"] and "Human review note." in result["content"]


def test_preservation_cannot_claim_text_outside_the_captured_page():
    with pytest.raises(ValueError, match="captured page"):
        PageUpdate("An actual original paragraph.", preserved_fragments=("invented",))
