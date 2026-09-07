"""Translate shared settings validation into the existing REST errors."""

from pathlib import Path

from fastapi import HTTPException

from openkb.application import settings
from openkb.application.settings import (
    read_global_config as read_global_config,
)
from openkb.application.settings import (
    read_kb_config as read_kb_config,
)
from openkb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest


def apply_kb_config_patch(kb_dir: Path, request: KbConfigPatchRequest) -> None:
    try:
        settings.apply_kb_config_patch(kb_dir, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def apply_global_config_patch(request: GlobalConfigPatchRequest) -> None:
    try:
        settings.apply_global_config_patch(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
