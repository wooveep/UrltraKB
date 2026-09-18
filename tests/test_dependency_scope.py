"""Dependency review narrows by structure while retaining conditions and fallback paths."""

import json

import pytest

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response


@pytest.mark.parametrize("cross_reference", [False, True])
def test_import_reviews_only_affected_sections_and_keeps_original_conditions(
    kb_dir, tmp_path, model_service, cross_reference
):
    source = tmp_path / "scoped.md"
    source.write_text(
        "# Guide\n\n"
        "## Backup requirements\n\nA verified backup is required before migration.\n\n"
        + (
            "## Migration\n\nSee Backup requirements before migration.\n\n"
            if cross_reference
            else ""
        )
        + "Run migrate --strict.\n\n"
        "## Metrics\n\nMetrics uses port 9342.\n\n"
        "## Retention\n\nAudit records are kept for 30 days."
    )
    reviews = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        stage = request["stage"]
        value = evidence_response(request)
        if stage == "facts":
            rows = []
            for unit, row in zip(request["units"], value["units"], strict=True):
                if "verified backup" in unit["text"]:
                    continue
                if unit["kind"] == "heading":
                    row.update(facts=[], empty_reason="Organizational heading")
                else:
                    topic = (
                        "Metrics"
                        if "9342" in unit["text"]
                        else ("Retention" if "30 days" in unit["text"] else "Migration")
                    )
                    for fact in row["facts"]:
                        fact["topic"] = topic
                rows.append(row)
            value = {"units": rows}
        elif stage == "planning":
            value = {
                "topics": [
                    {"name": label.lower(), "title": label, "kind": "concept", "members": [uid]}
                    for uid, label in request["topic_labels"].items()
                ]
            }
        elif stage == "dependencies":
            reviews.append(request)
            paths = {row["path"] for row in request["candidates"]}
            text = "\n".join(row["text"] for row in request["source"])
            if "concepts/migration" in paths:
                assert "verified backup" in text and "migrate --strict" in text
            # Every decision sees the omitted scope; keywords cannot prove that
            # another section is independent. Unrelated sections stay separate.
            assert not ("9342" in text and "30 days" in text)
            value = {
                "topics": [
                    {
                        "path": path,
                        "status": "dependent" if path == "concepts/migration" else "independent",
                        "reason": "Migration needs backup; metrics and retention are independent.",
                    }
                    for path in paths
                ]
            }
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert {row["path"] for request in reviews for row in request["candidates"]} == {
        "concepts/migration",
        "concepts/metrics",
        "concepts/retention",
    }
    assert not (kb_dir / "wiki/concepts/migration.md").exists()
    assert (kb_dir / "wiki/concepts/metrics.md").exists()
    assert (kb_dir / "wiki/concepts/retention.md").exists()


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


def test_newly_excluded_operation_propagates_without_rerolling_known_rejections(
    kb_dir, tmp_path, model_service
):
    from openkb.application.source_actions import continue_source

    source = tmp_path / "chain.md"
    source.write_text(
        "# Alpha\n\nAlpha requires a verified backup.\n\nRun Alpha.\n\n"
        "# Beta\n\nRun Beta after Alpha.\n\n"
        "# Metrics\n\nMetrics listens on port 9342."
    )
    reviewed = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        value = evidence_response(request)
        if request["stage"] == "facts":
            rows = []
            for unit, row in zip(request["units"], value["units"], strict=True):
                if "verified backup" in unit["text"]:
                    continue
                if unit["kind"] == "heading":
                    row.update(facts=[], empty_reason="Heading")
                else:
                    for fact in row["facts"]:
                        fact["topic"] = unit["headings"][-1]
                rows.append(row)
            value = {"units": rows}
        elif request["stage"] == "planning":
            value = {
                "topics": [
                    {"name": title.lower(), "title": title, "kind": "concept", "members": [uid]}
                    for uid, title in request["topic_labels"].items()
                ]
            }
        elif request["stage"] == "dependencies":
            missing_alpha = any(
                "concepts/alpha" in gap.get("items", []) for gap in request["omissions"]
            )
            value = {"topics": []}
            for row in request["candidates"]:
                path = row["path"]
                reviewed.append((path, missing_alpha))
                value["topics"].append(
                    {
                        "path": path,
                        "status": "dependent"
                        if path == "concepts/alpha" or (path == "concepts/beta" and missing_alpha)
                        else "independent",
                        "reason": "Check the controlled prerequisite chain against this omission.",
                    }
                )
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert not (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    assert (kb_dir / "wiki/concepts/metrics.md").exists()
    assert ("concepts/beta", True) in reviewed
    previous = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert all(
        json.loads(call["messages"][-1]["content"])["stage"] == "facts"
        for call in model_service[previous:]
    )
    assert not (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/beta.md").exists()


def test_unresolved_omitted_scope_cannot_be_overwritten_by_other_independence(
    kb_dir, tmp_path, model_service, monkeypatch
):
    from openkb.agent import dependency_preflight

    original = tmp_path / "two-gaps.md"
    original.write_text(
        "# First condition\n\nMissing condition one.\n\n"
        "# Second condition\n\nMissing condition two.\n\n"
        "# Operation\n\nRestart requires the applicable conditions."
    )
    generated = False
    capacity_checks = 0
    initial_fits = dependency_preflight.source_fits
    known = dependency_preflight.known_omissions

    def separate_omissions(parsed):
        return [{**row, "items": [item]} for row in known(parsed) for item in row.get("items", [])]

    monkeypatch.setattr(dependency_preflight, "known_omissions", separate_omissions)

    def fits(rows, settings, **kwargs):
        nonlocal capacity_checks
        if generated:
            capacity_checks += 1
            if capacity_checks == 1:
                return False
        return initial_fits(rows, settings, **kwargs)

    monkeypatch.setattr(dependency_preflight, "source_fits", fits)

    def respond(body):
        nonlocal generated
        payload = json.loads(body["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            for unit, row in zip(payload["units"], value["units"], strict=True):
                if unit["kind"] == "heading":
                    row.update(facts=[], empty_reason="Organizational heading")
            value["units"] = [
                row
                for unit, row in zip(payload["units"], value["units"], strict=True)
                if "Missing condition" not in unit["text"]
            ]
        if payload["stage"] == "generation":
            generated = True
        return value

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert capacity_checks == 1  # One joint decision; never split the missing conditions.
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    assert any(row["reason"] == "dependency_scope_unresolved" for row in result.omissions)
