"""Resolve the directly entered cloud OCR key without exposing it in settings."""

from __future__ import annotations

import os
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

if TYPE_CHECKING:
    from openkb.ocr.config import CloudSettings

OCR_API_KEY_ENV = "PADDLEOCR_API_KEY"


@dataclass(frozen=True)
class CloudCredential:
    api_key: str | None = field(default=None, repr=False)
    source: str = "unset"


def resolve_ocr_credential(
    kb_dir: Path | None = None, cloud: CloudSettings | None = None
) -> CloudCredential:
    """Use KB, process, then global credentials; retain legacy reference fallback.

    A directly entered key takes precedence over a legacy custom reference.
    Global settings report their own saved key, like the model key editor.
    """
    from openkb import config
    from openkb.config_state import active_values
    from openkb.locks import kb_read_lock

    if kb_dir and (captured := active_values(kb_dir)) is not None:
        return CloudCredential(**captured["ocr_credential"])
    with (
        kb_read_lock(kb_dir / ".openkb") if kb_dir else nullcontext(),
        config._with_global_config_lock(),
    ):
        layers = []
        if kb_dir:
            layers.extend([("kb", dotenv_values(kb_dir / ".env")), ("environment", os.environ)])
        layers.append(("global", dotenv_values(config.GLOBAL_CONFIG_DIR / ".env")))
        keys = [OCR_API_KEY_ENV]
        if cloud and cloud.credential_env not in keys:
            keys.append(cloud.credential_env)
        for key in keys:
            for source, values in layers:
                if value := values.get(key):
                    return CloudCredential(value, source)
    return CloudCredential()
