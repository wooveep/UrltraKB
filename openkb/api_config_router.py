"""Global-config REST endpoints (GET/PATCH /api/v1/config).

An APIRouter (sibling of api_graph.py / api_output.py) so api.py stays under the
per-file line gate (tests/test_file_size.py). Global writes are serialized by
save_global_config's own lock (openkb/config.py), NOT the per-KB mutation lock,
so these endpoints need no create_app closure and extract cleanly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict

from openkb.api_config import apply_global_config_patch, read_global_config
from openkb.api_helpers import require_bearer_token
from openkb.api_models import GlobalConfigPatchRequest, GlobalConfigResponse
from openkb.api_ocr_config import router as ocr_config_router

config_router = APIRouter()


class ImageConnectionTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kb: str | None = None


@config_router.post("/api/v1/config/image/test")
async def image_connection_test(
    request: ImageConnectionTest, _: None = Depends(require_bearer_token)
):
    from openkb.api_helpers import _resolve_kb
    from openkb.application.image_understanding import test_image_connection

    root = await run_in_threadpool(_resolve_kb, request.kb) if request.kb else None
    return await test_image_connection(root)


@config_router.get("/api/v1/config", response_model=GlobalConfigResponse)
async def global_config_get(
    _: None = Depends(require_bearer_token),
) -> GlobalConfigResponse:
    return await run_in_threadpool(read_global_config)


@config_router.patch("/api/v1/config", response_model=GlobalConfigResponse)
async def global_config_patch(
    request: GlobalConfigPatchRequest,
    _: None = Depends(require_bearer_token),
) -> GlobalConfigResponse:
    # Both operations can wait for global-state recovery or another writer.
    await run_in_threadpool(apply_global_config_patch, request)
    return await run_in_threadpool(read_global_config)


config_router.include_router(ocr_config_router)
