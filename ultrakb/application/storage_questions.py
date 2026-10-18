"""Commit completed question outputs against their exact evidence publication."""

from collections.abc import Callable

from ultrakb.knowledge_scope import KnowledgeScope
from ultrakb.lint import wiki_targets
from ultrakb.log import appended_log

from .answers import exploration_markdown, exploration_slug
from .conversations import AnswerResult
from .publications import publish_knowledge_changes


def finish_storage_question(
    scope: KnowledgeScope,
    question: str,
    final: dict,
    *,
    save: bool,
    check_cancelled: Callable[[], None],
) -> AnswerResult:
    bound = scope.knowledge
    assert bound is not None
    paths = bound.files()
    previous_log = bound.read("log.md").decode("utf-8") if "log.md" in paths else None
    changes: dict[str, bytes | None] = {
        "log.md": appended_log(previous_log, "query", question).encode("utf-8")
    }
    relative = None
    if save and final["answer"]:
        slug = exploration_slug(question, unique=True)
        relative = f"explorations/{slug}.md"
        counter = 1
        while relative in paths:
            relative = f"explorations/{slug}-{counter}.md"
            counter += 1
        changes[relative] = exploration_markdown(
            question, final["answer"], wiki_targets(paths)
        ).encode("utf-8")
    outcome = publish_knowledge_changes(
        bound,
        changes,
        page_inputs={relative: tuple(unit.unit_revision_id for unit in bound.input_units())}
        if relative
        else None,
        check_cancelled=check_cancelled,
    )
    saved = scope.wiki_dir / relative if relative and outcome.publication else None
    return AnswerResult(
        "completed" if outcome.status == "published" else "blocked",
        final["answer"],
        saved_path=str(saved) if saved else None,
        resources=(str(saved),) if saved else (),
        changes=(f"created: wiki/{relative}",) if saved else (),
        usage=final.get("usage"),
        answer_outcome=final.get("answer_outcome"),
        citations=final.get("citations"),
        unfinished=tuple(final.get("unfinished", ()))
        + (
            ("save answer" if save else "record question log",)
            if outcome.status == "needs_review"
            else ()
        ),
        error="Knowledge changed; review the retained question result"
        if outcome.status == "needs_review"
        else None,
        result_publication_id=outcome.publication.publication_id if outcome.publication else None,
        projection=outcome.projection,
        candidate_revision_id=outcome.candidate_revision_id,
        reason_code=outcome.reason_code,
    )
