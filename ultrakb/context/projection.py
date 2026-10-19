"""A fixed reading selection becomes a reconstructible, namespaced navigation forest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ultrakb.application.query_views import QuerySelection

PROJECTION_VERSION = "ultrakb-knowledge-forest-v2"


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ProjectionIdentity:
    key: str
    tree_id: str


def ensure_projection(selection: QuerySelection, model_identity: dict) -> ProjectionIdentity:
    if len(selection.views) != 1:
        raise ValueError("Knowledge retrieval requires one bound collection")
    if bound := selection.views[0].scope.knowledge:
        revision = bound.navigation_revision()
        return ProjectionIdentity(revision.input_fingerprint, revision.tree_id)
    raise ValueError("Knowledge retrieval requires a main-store publication")
