"""Bind each visual credential to its complete connection and configuration layer."""

from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from openkb.sources import content_id
from openkb.vision.config import IMAGE_API_KEY_ENV, IMAGE_CONNECTION_ENV, vision_settings


@dataclass(frozen=True)
class ImageCredential:
    api_key: str | None = field(default=None, repr=False)
    source: str = "unset"


def resolve_image_credential(kb_dir: Path | None = None) -> ImageCredential:
    from openkb import config
    from openkb.config_state import active_values

    if kb_dir and (captured := active_values(kb_dir)) is not None:
        return ImageCredential(**captured["image_credential"])
    if kb_dir:
        effective, sources = config.resolve_effective_config(kb_dir)
        scope = sources["image_understanding"]
    else:
        effective, scope = config.load_global_config(), "global"
    settings = vision_settings(effective.get("image_understanding"))
    if settings.connection == "reuse_main":
        if kb_dir:
            bundle = config.resolve_credential_bundle(kb_dir)
            return ImageCredential(bundle.api_key, "main")
        values = dotenv_values(config.GLOBAL_CONFIG_DIR / ".env")
        return ImageCredential(values.get("LLM_API_KEY"), "main")
    root = kb_dir if scope == "kb" else config.GLOBAL_CONFIG_DIR
    assert root is not None
    values = dotenv_values(root / ".env")
    if values.get(IMAGE_CONNECTION_ENV) == content_id(settings.identity()) and (
        key := values.get(IMAGE_API_KEY_ENV)
    ):
        return ImageCredential(key, scope)
    return ImageCredential()


def credential_binding(kb_dir: Path | None) -> str:
    from openkb import config

    effective = (
        config.resolve_effective_config(kb_dir)[0] if kb_dir else config.load_global_config()
    )
    return content_id(vision_settings(effective.get("image_understanding")).identity())
