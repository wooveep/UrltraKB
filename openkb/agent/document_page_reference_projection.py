"""Project only source-bound mentions relevant to one planned page."""

from __future__ import annotations

from typing import Any

from openkb.agent.document_plan import PagePlan
from openkb.agent.document_plan_annotations import ExternalReference


def page_external_references(page: PagePlan, settings: dict[str, Any]) -> list[dict[str, Any]]:
    values = settings.get("_document_external_references", [])
    if not isinstance(values, list):
        raise ValueError("Invalid page external reference projection")
    return [
        reference.to_dict()
        for value in values
        for reference in [ExternalReference.from_dict(value)]
        if page.key in reference.affected_pages
    ]
