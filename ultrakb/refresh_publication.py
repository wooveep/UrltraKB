"""Publish a whole refreshed view against an exact input and knowledge generation."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RefreshResult:
    status: str
    view_id: str
    knowledge_revision_id: str | None = None
    proposal_id: str | None = None
    message: str = ""
    resources: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    publication_id: str | None = None
    projection_pending: bool = False
