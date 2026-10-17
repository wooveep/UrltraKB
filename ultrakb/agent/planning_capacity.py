"""Admit complete topic plans while preserving every native reading locator."""

import json
from collections.abc import Callable

from ultrakb.model_tokenizer import ContextCapacityError
from ultrakb.source_navigation import planning_navigation


def capacity_error(detail: str, fits) -> ContextCapacityError:
    diagnostic = getattr(fits, "last_failure", None)
    if diagnostic:
        detail += (
            "\nComplete request local measurements (not provider billed tokens): "
            + json.dumps(diagnostic, ensure_ascii=False)
        )
    return ContextCapacityError(detail)


def navigation_messages(doc_name: str, doc_id: str, structure: list[dict]) -> tuple[dict, dict]:
    """Keep the historical full request and an evidence-neutral compact alternative."""
    nodes = planning_navigation(structure)
    header = (
        f"Generated navigation for document {doc_name} (doc_id: {doc_id}); "
        "these titles and navigation summaries are planning aids, not original evidence. "
        "parent_node_id identifies the retained parent section.\n"
    )
    compact = [{k: v for k, v in node.items() if k != "navigation_summary"} for node in nodes]
    return (
        {"role": "user", "content": header + json.dumps(nodes, ensure_ascii=False)},
        {"role": "user", "content": header + json.dumps(compact, ensure_ascii=False)},
    )


def fit_planning_messages(
    messages: list[dict],
    fits: Callable[[list[dict]], bool],
    *,
    compact_document_message: dict | None = None,
) -> list[dict]:
    """Measure the full wire request; only generated navigation summaries are optional.

    Both the first request and a later correction use this admission. Originals,
    native titles/ranges/hierarchy, overview and existing targets are never sliced.
    Fitting historical messages remain byte-identical for checkpoint reuse.
    """
    if fits(messages):
        return messages
    if compact_document_message is not None:
        candidate = [messages[0], compact_document_message, *messages[2:]]
        if fits(candidate):
            return candidate
    raise capacity_error(
        "Complete compile planning input cannot fit captured model capacity even without "
        "optional navigation summaries. All native locators, original input and completed "
        "index/summary checkpoints are retained; use a model capacity that fits the complete "
        "planning catalog.",
        fits,
    )


def preflight_planning(
    system_message,
    document_message,
    *,
    wiki_dir,
    entity_types,
    fits,
    long=False,
    compact_document_message=None,
) -> bool:
    """Choose a known catalog partition path before summary generation costs accrue.

    An empty generated overview is only a lower bound. The exact accepted
    overview and every correction are still admitted after summary completion.
    """
    from ultrakb.agent.compile_messages import planning_messages
    from ultrakb.agent.compiler import _read_concept_briefs, _read_entity_briefs
    from ultrakb.agent.partition_planning import partition_messages
    from ultrakb.agent.planning_identity import identity_messages

    messages = planning_messages(
        system_message,
        document_message,
        "",
        concept_briefs=_read_concept_briefs(wiki_dir),
        entity_briefs=_read_entity_briefs(wiki_dir),
        entity_types=entity_types,
        long=long,
    )
    try:
        fit_planning_messages(messages, fits, compact_document_message=compact_document_message)
    except ContextCapacityError:
        minimums = [
            partition_messages(system_message, [{"node_id": "work"}], [], entity_types),
            identity_messages(
                system_message,
                {"id": "candidate", "kind": "concepts", "name": "topic", "title": "Topic"},
                [],
            ),
        ]
        if not all(fits(minimum) for minimum in minimums):
            raise capacity_error(
                "Compile planning fixed rules, schema, JSON protocol and reserved output "
                "cannot fit captured capacity even for one work ID. No summary request was "
                "sent; the original and existing checkpoints are retained.",
                fits,
            ) from None
        return True
    return False
