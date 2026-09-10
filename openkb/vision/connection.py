"""Resolve a task-owned connection without configuring any process-global SDK state."""

from dataclasses import dataclass, field
from pathlib import Path

from openkb.sources import content_id
from openkb.vision.config import VisionSettings, vision_settings
from openkb.vision.credentials import resolve_image_credential


@dataclass(frozen=True)
class VisionConnection:
    settings: VisionSettings
    api_key: str | None = field(default=None, repr=False)
    error: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def identity(self):
        identity = self.settings.identity()
        if self.extra_headers:
            identity["header_identity"] = content_id(self.extra_headers)
        return identity

    @property
    def id(self):
        return content_id(self.identity)

    def readiness(self):
        if self.error:
            return self.error
        if not self.settings.model or not self.identity["endpoint"]:
            return "image_connection_not_configured"
        if self.settings.authentication == "api_key" and not self.api_key:
            return "image_credentials_missing"
        if not self.settings.supports_images:
            return "image_capability_not_declared"
        return None


def resolve_connection(kb_dir: Path | None = None) -> VisionConnection:
    from openkb import config

    effective = (
        config.resolve_effective_config(kb_dir)[0] if kb_dir else config.load_global_config()
    )
    settings = vision_settings(effective.get("image_understanding"))
    credential = resolve_image_credential(kb_dir)
    headers = {}
    if settings.connection == "reuse_main":
        if kb_dir:
            bundle = config.resolve_credential_bundle(kb_dir)
            endpoint = bundle.base_url
            headers = dict(bundle.extra_headers)
        else:
            from dotenv import dotenv_values

            endpoint = dotenv_values(config.GLOBAL_CONFIG_DIR / ".env").get("OPENAI_API_BASE")
            headers = config.resolve_per_request_overrides(effective)[0]
        model = effective.get("model", config.DEFAULT_CONFIG["model"])
        provider, separator, name = model.partition("/")
        if not separator:
            provider, name = "openai", model
        if provider not in {"openai", "anthropic", "ollama"}:
            return VisionConnection(settings, error="image_main_protocol_unsupported")
        settings = VisionSettings.model_validate(
            {
                **settings.model_dump(),
                "provider": provider,
                "model": name,
                "endpoint": endpoint,
                "authentication": "none"
                if provider == "ollama" and not credential.api_key
                else "api_key",
            }
        )
    return VisionConnection(settings, credential.api_key, extra_headers=headers)


def capability_path(connection: VisionConnection) -> Path:
    from openkb import config

    return config.GLOBAL_CONFIG_DIR / "image-capabilities" / f"{connection.id}.json"


def capability_verified(connection: VisionConnection) -> bool:
    import json

    path = capability_path(connection)
    try:
        return not path.is_symlink() and json.loads(path.read_text()) == {
            "connection": connection.identity,
            "image_input": True,
        }
    except (ValueError, OSError):
        return False
