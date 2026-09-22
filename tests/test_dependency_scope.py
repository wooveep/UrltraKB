"""Dependency review narrows by structure while retaining conditions and fallback paths."""

import json

import pytest

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response


@pytest.mark.parametrize("cross_reference", [False, True])
def test_import_binds_conditions_only_to_affected_pages(
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
    page_evidence = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        stage = request["stage"]
        value = evidence_response(request)
        if stage == "planning":
            blocks = request["evidence"]["blocks"]

            def ranges(*texts):
                return [
                    [row["order"], row["order"] + 1]
                    for row in blocks
                    if row["text"].lstrip("# ").strip() in texts or row["text"] in texts
                ]

            def basis(selected):
                return "\n".join(
                    next(row["text"] for row in blocks if row["order"] == index)
                    for start, end in selected
                    for index in range(start, end)
                )

            guide = ranges("Guide")
            backup = ranges(
                "Backup requirements", "A verified backup is required before migration."
            )
            migration = ranges(
                "Migration", "See Backup requirements before migration.", "Run migrate --strict."
            )
            metrics = ranges("Metrics", "Metrics uses port 9342.")
            retention = ranges("Retention", "Audit records are kept for 30 days.")
            target = request["target"]
            return {
                "overview": {
                    "text": "Migration, metrics and retention guidance.",
                    "ranges": target.get(
                        "ranges", [[target["target_start"], target["target_end"]]]
                    ),
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "migration",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/migration",
                        "title": "Migration",
                        "purpose": "Run migration with its prerequisite.",
                        "subject_ranges": migration,
                        "necessary_context": [
                            {
                                "relation": "applicable_condition",
                                "ranges": backup,
                                "basis": basis(backup),
                                "basis_ranges": backup,
                            }
                        ],
                    },
                    {
                        "local_key": "metrics",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/metrics",
                        "title": "Metrics",
                        "purpose": "Metrics listener configuration.",
                        "subject_ranges": metrics,
                        "necessary_context": [],
                    },
                    {
                        "local_key": "retention",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/retention",
                        "title": "Retention",
                        "purpose": "Audit record retention period.",
                        "subject_ranges": retention,
                        "necessary_context": [],
                    },
                ],
                "source_only": [
                    {
                        "ranges": guide,
                        "reason": "Document title is source-only navigation metadata.",
                    }
                ],
                "unresolved": [],
                "resolutions": [],
            }
        if stage in {"generation", "verification"}:
            title = request["page"]["title"]
            text = "\n".join(row["text"] for row in request["evidence"]["blocks"])
            page_evidence.append((title, text))
            if stage == "generation":
                content = {
                    "Migration": "A verified backup is required before running migrate --strict.",
                    "Metrics": "Metrics uses port 9342.",
                    "Retention": "Audit records are kept for 30 days.",
                }[title]
                return {
                    "content": content,
                    "covered": [row["id"] for row in request["occurrences"]],
                }
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    migration_evidence = [text for title, text in page_evidence if title == "Migration"]
    assert migration_evidence and all("verified backup" in text for text in migration_evidence)
    assert all("migrate --strict" in text for text in migration_evidence)
    assert all("9342" not in text and "30 days" not in text for text in migration_evidence)
    assert (kb_dir / "wiki/concepts/migration.md").exists()
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


def test_unresolved_prerequisite_blocks_chain_without_rerolling_plan(
    kb_dir, tmp_path, model_service
):
    from openkb.application.source_actions import continue_source

    source = tmp_path / "chain.md"
    source.write_text(
        "# Alpha\n\nAlpha requires a verified backup.\n\nRun Alpha.\n\n"
        "# Beta\n\nRun Beta after Alpha.\n\n"
        "# Metrics\n\nMetrics listens on port 9342."
    )
    plans = []

    def respond(body):
        request = json.loads(body["messages"][-1]["content"])
        value = evidence_response(request)
        if request["stage"] == "planning":
            plans.append(request)
            blocks = request["evidence"]["blocks"]

            def ranges(*texts):
                return [
                    [row["order"], row["order"] + 1]
                    for row in blocks
                    if row["text"].lstrip("# ").strip() in texts or row["text"] in texts
                ]

            def basis(selected):
                return "\n".join(
                    next(row["text"] for row in blocks if row["order"] == index)
                    for start, end in selected
                    for index in range(start, end)
                )

            alpha = ranges("Alpha", "Alpha requires a verified backup.", "Run Alpha.")
            beta = ranges("Beta", "Run Beta after Alpha.")
            metrics = ranges("Metrics", "Metrics listens on port 9342.")
            target = request["target"]
            return {
                "overview": {
                    "text": "Alpha, Beta and metrics procedures.",
                    "ranges": target.get(
                        "ranges", [[target["target_start"], target["target_end"]]]
                    ),
                    "limitations": ["The required verified backup is not available."],
                },
                "page_changes": [
                    {
                        "local_key": "alpha",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/alpha",
                        "title": "Alpha",
                        "purpose": "Run Alpha after a verified backup.",
                        "subject_ranges": alpha,
                        "necessary_context": [],
                    },
                    {
                        "local_key": "beta",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/beta",
                        "title": "Beta",
                        "purpose": "Run Beta after Alpha.",
                        "subject_ranges": beta,
                        "necessary_context": [
                            {
                                "relation": "explicit_reference",
                                "ranges": alpha,
                                "basis": basis(alpha),
                                "basis_ranges": alpha,
                            }
                        ],
                    },
                    {
                        "local_key": "metrics",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/metrics",
                        "title": "Metrics",
                        "purpose": "Metrics listener configuration.",
                        "subject_ranges": metrics,
                        "necessary_context": [],
                    },
                ],
                "source_only": [],
                "unresolved": [
                    {
                        "location": alpha,
                        "problem_type": "missing_prerequisite",
                        "missing_target": "a verified backup",
                        "affected_pages": ["alpha", "beta"],
                        "blocking": True,
                        "reason": "The prerequisite backup has not been supplied.",
                    }
                ],
                "resolutions": [],
            }
        if request["stage"] == "generation":
            return {
                "content": "Metrics listens on port 9342.",
                "covered": [row["id"] for row in request["occurrences"]],
            }
        return value

    model_service.respond = respond
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert not (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    assert (kb_dir / "wiki/concepts/metrics.md").exists()
    assert plans and any(
        row["reason"] == "unresolved_prerequisite_blocked" for row in result.omissions
    )
    previous = len(model_service)
    continued = continue_source(kb_dir, result.source_id, version_id=result.input_version)
    assert continued.knowledge_compilation == "completed", continued
    assert len(model_service) == previous
    assert not (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/beta.md").exists()


def test_multiple_unresolved_conditions_keep_operation_blocked(kb_dir, tmp_path, model_service):
    original = tmp_path / "two-gaps.md"
    original.write_text(
        "# First condition\n\nMissing condition one.\n\n"
        "# Second condition\n\nMissing condition two.\n\n"
        "# Operation\n\nRestart requires the applicable conditions."
    )

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "planning":
            blocks = payload["evidence"]["blocks"]

            def ranges(*texts):
                return [
                    [row["order"], row["order"] + 1]
                    for row in blocks
                    if row["text"].lstrip("# ").strip() in texts or row["text"] in texts
                ]

            def basis(selected):
                return "\n".join(
                    next(row["text"] for row in blocks if row["order"] == index)
                    for start, end in selected
                    for index in range(start, end)
                )

            first = ranges("First condition", "Missing condition one.")
            second = ranges("Second condition", "Missing condition two.")
            operation = ranges("Operation", "Restart requires the applicable conditions.")
            target = payload["target"]
            return {
                "overview": {
                    "text": "Restart conditions remain unresolved.",
                    "ranges": target.get(
                        "ranges", [[target["target_start"], target["target_end"]]]
                    ),
                    "limitations": ["Both applicable conditions are missing."],
                },
                "page_changes": [
                    {
                        "local_key": "operation",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/operation",
                        "title": "Operation",
                        "purpose": "Restart only after the applicable conditions are known.",
                        "subject_ranges": operation,
                        "necessary_context": [
                            {
                                "relation": "applicable_condition",
                                "ranges": first,
                                "basis": basis(first),
                                "basis_ranges": first,
                            },
                            {
                                "relation": "applicable_condition",
                                "ranges": second,
                                "basis": basis(second),
                                "basis_ranges": second,
                            },
                        ],
                    }
                ],
                "source_only": [],
                "unresolved": [
                    {
                        "location": first,
                        "problem_type": "missing_prerequisite",
                        "missing_target": "condition one",
                        "affected_pages": ["operation"],
                        "blocking": True,
                        "reason": "The first applicable condition is missing.",
                    },
                    {
                        "location": second,
                        "problem_type": "missing_prerequisite",
                        "missing_target": "condition two",
                        "affected_pages": ["operation"],
                        "blocking": True,
                        "reason": "The second applicable condition is missing.",
                    },
                ],
                "resolutions": [],
            }
        return value

    model_service.respond = respond
    result = import_document(kb_dir, original)
    assert result.knowledge_compilation == "completed", result
    assert not any(
        json.loads(call["messages"][-1]["content"])["stage"] == "generation"
        for call in model_service
    )
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))
    assert {
        "The first applicable condition is missing.",
        "The second applicable condition is missing.",
    } <= {row["reason"] for row in result.omissions}
