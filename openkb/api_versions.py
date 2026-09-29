"""Version clarification HTTP actions backed by the common durable application API."""

import json
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import field_validator
from starlette.concurrency import run_in_threadpool

from openkb.api_helpers import _resolve_kb, require_bearer_token
from openkb.api_views import ViewRequest, resolve_api_scope
from openkb.application.version_review import (
    cancel_version_review,
    list_version_reviews,
    read_version_review,
    resume_version_review,
    review_source_version,
    supplement_version_reviews,
)
from openkb.source_records import RecordId
from openkb.view_records import SourceMetadata

versions_router = APIRouter(dependencies=[Depends(require_bearer_token)])


class ReviewRequest(ViewRequest):
    kb: str = "default"
    review_id: RecordId


class SourceReviewRequest(ViewRequest):
    kb: str = "default"
    source_id: RecordId


class SupplementRequest(ViewRequest):
    kb: str = "default"
    updates: dict[RecordId, SourceMetadata]

    @field_validator("updates", mode="before")
    @classmethod
    def parse_metadata(cls, value):
        if isinstance(value, dict):
            return {
                key: SourceMetadata.model_validate_json(json.dumps(metadata))
                for key, metadata in value.items()
            }
        return value


async def _invoke(operation, request, *args):
    root = await run_in_threadpool(_resolve_kb, request.kb)
    scope = await resolve_api_scope(root, request.view_id)
    try:
        return await run_in_threadpool(operation, root, *args, scope=scope)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@versions_router.get("/api/v1/version-reviews")
async def pending(kb: str = "default", view_id: str | None = None):
    root = await run_in_threadpool(_resolve_kb, kb)
    scope = await resolve_api_scope(root, view_id)
    return await run_in_threadpool(list_version_reviews, root, scope=scope)


@versions_router.post("/api/v1/version-review")
async def read(request: ReviewRequest):
    return await _invoke(read_version_review, request, request.review_id)


@versions_router.post("/api/v1/version-review/open")
async def open_review(request: SourceReviewRequest):
    return await _invoke(review_source_version, request, request.source_id)


@versions_router.post("/api/v1/version-reviews/supplement")
async def supplement(request: SupplementRequest):
    return await _invoke(supplement_version_reviews, request, request.updates)


@versions_router.post("/api/v1/version-review/cancel")
async def cancel(request: ReviewRequest):
    return await _invoke(cancel_version_review, request, request.review_id)


@versions_router.post("/api/v1/version-review/resume")
async def resume(request: ReviewRequest):
    return asdict(await _invoke(resume_version_review, request, request.review_id))
