"""Confirm native stop deliveries through the shared application and exact generation."""

from ultrakb.application.pending import stop_pending_job
from ultrakb.application.views import view_scope
from ultrakb.catalog_schema import UnsupportedCatalogWriter
from ultrakb.lifecycle import KnowledgeBaseRemoved, expected_generation


def confirm_stops(root, stops):
    for request in stops:
        if request.generation is None:
            continue  # Old summaries without a generation cannot mutate an existing KB.
        try:
            with expected_generation(root, request.generation):
                scope = view_scope(root, request.view_id) if request.view_id is not None else None
                stop_pending_job(
                    root, request.job_id, request.dispatch_id, request.generation, scope=scope
                )
        except (KnowledgeBaseRemoved, UnsupportedCatalogWriter):
            pass  # A replaced or unsupported KB cannot accept the old delivery.
