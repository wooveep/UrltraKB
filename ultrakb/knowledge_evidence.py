"""Read page provenance from its fixed immutable main-store publication."""

from ultrakb.knowledge_scope import KnowledgeScope


def page_evidence(scope: KnowledgeScope, page: str) -> dict:
    if scope.knowledge is None:
        return {
            "validity": "current",
            "knowledge_revision_id": None,
            "source_revision_ids": (),
            "refresh_reasons": (),
        }
    from ultrakb.storage.knowledge_validity import publication_validity

    native = scope.reading_view.native_inputs.get(page)
    status = publication_validity(
        scope.knowledge, page=page, native_unit=native[0] if native else None
    )
    if scope.historical:
        status = {**status, "validity": "historical", "refresh_reasons": ()}
    return {
        key: status[key]
        for key in (
            "validity",
            "knowledge_revision_id",
            "source_revision_ids",
            "refresh_reasons",
        )
    }
