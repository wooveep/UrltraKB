"""Explicit view selection shared by HTTP read and write adapters."""

import asyncio
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

from openkb.application.views import view_scope
from openkb.knowledge_scope import KnowledgeScope
from openkb.source_records import ViewId


class ViewRequest(BaseModel):
    view_id: ViewId | None = None


async def resolve_api_scope(root: Path, view_id: str | None) -> KnowledgeScope | None:
    if view_id is None:
        return None
    try:
        return await asyncio.to_thread(view_scope, root, view_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Unknown knowledge view") from exc
