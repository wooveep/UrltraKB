"""Dependency review narrows by structure while retaining conditions and fallback paths."""


import pytest


def _review_scope(*, preamble="", pointer="", gap="b", link="", omitted_text=None):
    from openkb.agent.dependency_scope import review_scopes

    rows = []
    for bid, kind, text in [
        ("p", "paragraph", preamble),
        ("ha", "heading", "# Alpha"),
        ("a", "paragraph", "Alpha operates independently. " + pointer),
        ("hb", "heading", "# Beta"),
        ("b", "paragraph", omitted_text or "Beta requires a backup."),
    ]:
        if text:
            rows.append(
                {
                    "reference": {"block_id": bid},
                    "location": {"kind": "text"},
                    "kind": kind,
                    "text": text,
                }
            )
    facts = [
        {"scope": {"block_id": "a"}, "topic": "Alpha"},
        {"scope": {"block_id": "b"}, "topic": "Beta"},
    ]
    candidates = [{"path": "concepts/alpha", "facts": facts[:1], "content": "Alpha " + link}]
    omissions = [{"stage": "facts", "items": [gap]}] if gap else [{"stage": "parsing"}]
    return review_scopes(rows, omissions, candidates, facts, [])


def test_unrelated_headings_still_require_a_semantic_decision():
    batches = _review_scope()
    assert len(batches) == 1
    assert {row["reference"]["block_id"] for row in batches[0][0]} >= {"a", "b"}


@pytest.mark.parametrize(
    "options",
    [
        {"gap": None},
        {"gap": "unlocated"},
        {"pointer": "See the above conditions."},
        {"pointer": "See Beta."},
        {"pointer": "See [conditions](#beta)."},
        {"omitted_text": "Beta contains a backup condition required before Alpha."},
        {"preamble": "A verified backup is required for all operations."},
        {"preamble": "A verified backup is required.", "gap": "p"},
    ],
)
def test_unknown_references_and_shared_conditions_remain_in_review(options):
    batches = _review_scope(**options)
    assert len(batches) == 1
    assert (
        any(row["reference"]["block_id"] == "b" for row in batches[0][0])
        or options.get("gap") == "p"
    )
