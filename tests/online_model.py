"""Explicit, shared configuration entry point for real-provider acceptance runs.

Use OPENKB_TEST_MODEL_KB or an explicit config_kb; never borrow credentials from
another hardcoded KB. This module does not send requests, write KB files, or
change process credentials. Call the normal production entry point with settings
and bundle from the returned profile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openkb.config import (
    LlmCredentialBundle,
    compilation_model_options,
    resolve_credential_bundle,
    resolve_effective_config,
    validate_runtime_config,
)


@dataclass(frozen=True)
class OnlineModel:
    config_kb: Path
    settings: dict[str, Any] = field(repr=False)
    bundle: LlmCredentialBundle = field(repr=False)

    def description(self) -> dict[str, Any]:
        """Allowlisted metadata, without credentials, headers, or endpoint URLs."""
        return {
            "config_kb": str(self.config_kb),
            "model": self.settings["model"],
            "planning_options": compilation_model_options(self.settings, stage="planning"),
            "credential_present": bool(self.bundle.api_key),
            "custom_endpoint": bool(self.bundle.base_url),
        }


def load_online_model(config_kb: str | Path | None = None) -> OnlineModel:
    """Resolve settings and request credentials from the same explicit KB.

    Missing configuration is a setup failure, never a successful/skipped online
    run. The production resolver retains KB > environment > global precedence.
    Ordinary mocked tests do not use this opt-in entry point.
    """
    selected = config_kb if config_kb is not None else os.environ.get("OPENKB_TEST_MODEL_KB")
    if selected is None or not str(selected).strip():
        raise ValueError("Set OPENKB_TEST_MODEL_KB or pass config_kb for an online test")
    kb = Path(selected).expanduser().resolve()
    if not (kb / ".openkb" / "config.yaml").is_file():
        raise ValueError("Online model KB must contain .openkb/config.yaml")
    settings, _ = resolve_effective_config(kb)
    validate_runtime_config(settings)
    model = settings.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Online model KB has no valid model")
    bundle = resolve_credential_bundle(kb)
    if not bundle.api_key:
        raise ValueError(
            "Online model credentials are unavailable in the selected KB configuration"
        )
    return OnlineModel(kb, settings, bundle)
