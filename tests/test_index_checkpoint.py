"""Unrelated manual pages do not enter a native import's protected-change review."""

import pytest


@pytest.mark.parametrize("rewrite_manual", [False, True])
def test_manual_page_review_is_limited_to_changed_pages(rewrite_manual):
    from ultrakb.storage.generated_baselines import protected_knowledge_pages

    manual = "concepts/manual-note.md"
    current = {manual: "human-page-digest"}
    candidate = {
        **current,
        "summaries/gardening.md": "new-summary-digest",
        "sources/gardening.okbi": "retained-index-digest",
    }
    if rewrite_manual:
        candidate[manual] = "proposed-manual-page-digest"
    assert protected_knowledge_pages(current, candidate, {}) == (
        {manual} if rewrite_manual else set()
    )
