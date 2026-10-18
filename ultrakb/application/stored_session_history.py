"""Build the shared conversation display DTO from committed main-store records."""

from ultrakb.agent.answer_text import visible_answer
from ultrakb.mutation import RecoveryRequired
from ultrakb.storage.session_catalog import SessionCatalog
from ultrakb.storage.session_result import restore_trace

from .answer_records import CompletedAnswer
from .conversations import ConversationView


def restored_answer_citations(scope, snapshot):
    """Rebind only a trusted committed result, preserving its original provenance."""
    from ultrakb.storage.database import DatabaseIdentity

    from .answer_citations import validate_citation_snapshot
    from .views import storage_scope

    if snapshot is None:
        return None
    validate_citation_snapshot(snapshot)
    assert scope.knowledge is not None
    current = scope.knowledge.identity
    references, publications = [], {}
    for reference in snapshot["references"]:
        original = reference.get("storage_identity")
        if original is None:
            references.append(reference)
            continue
        try:
            previous = DatabaseIdentity(**original)
        except (TypeError, KeyError) as error:
            raise RecoveryRequired("Committed answer storage identity is invalid") from error
        if (
            previous.kb_id != current.kb_id
            or not isinstance(previous.generation, str)
            or not previous.generation
        ):
            raise RecoveryRequired("Committed answer provenance belongs to another knowledge base")
        if previous == current:
            references.append(reference)
            continue
        publication, view = reference.get("publication_id"), reference.get("view_id")
        if not all(isinstance(value, str) and value for value in (publication, view)):
            raise RecoveryRequired("Committed answer lacks its exact historical publication")
        key = (publication, view)
        if key not in publications:
            selected = storage_scope(
                scope.kb_dir, current, publication_id=publication, view_id=view
            )
            publications[key] = selected.reading_view.files
        digest = reference.get("digest")
        if not isinstance(digest, str) or publications[key].get(reference["path"]) != digest:
            raise RecoveryRequired("Committed answer no longer matches its retained source")
        references.append(
            {
                **reference,
                "storage_identity": {"kb_id": current.kb_id, "generation": current.generation},
                "provenance_storage_identity": original,
            }
        )
    return {**snapshot, "references": references}


def read_stored_conversation(scope, session_id, **page):
    assert scope.knowledge is not None
    history = SessionCatalog(scope.kb_dir, scope.knowledge.identity).read(
        session_id, view_id=scope.view_id, **page
    )
    turns, answers = [], []
    for turn, metadata in zip(history.turns, history.metadata, strict=True):
        messages = {item["role"]: item["content"] for item in turn["messages"]}
        original = (messages["user"], messages["assistant"])
        turns.append(original)
        result = metadata.get("result", {})
        answers.append(
            CompletedAnswer(
                turn["turn_id"],
                original[0],
                visible_answer(original[1]),
                result.get("usage"),
                tuple(restore_trace(metadata["trace"], original[1])) if metadata else (),
                result.get("answer_outcome"),
                restored_answer_citations(scope, result.get("citations")),
                str(scope.kb_dir),
                scope.view_id,
            )
        )
    return ConversationView(
        history.session_id,
        history.title,
        history.model,
        history.language,
        tuple(turns),
        history.version,
        history.pending,
        None,
        tuple(answer.outcome for answer in answers),
        tuple(answers),
        created_at=history.created_at,
        total_turns=history.total_turns,
        start_ordinal=history.start_ordinal,
        older_before=history.older_before,
        next_pending=history.next_pending,
    )
