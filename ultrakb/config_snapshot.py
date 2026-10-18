"""Read an immutable execution recipe without migration or journal recovery."""

import json
import os
from dataclasses import asdict
from importlib import import_module
from pathlib import Path

from dotenv import dotenv_values

from ultrakb import config
from ultrakb.config_state import ConfigSnapshot, live_configuration


def initialize_sdk_environment() -> None:
    """Resolve the SDK's lazy dotenv dependency before preparing or capturing settings."""
    import_module("pageindex")


def read_snapshot(kb_dir: Path, **wait_options) -> ConfigSnapshot:
    # PageIndex initializes dotenv on import. Freeze the actual SDK environment,
    # including lazy/fresh worker entry, before resolving any credentials.
    initialize_sdk_environment()
    paths = (
        kb_dir / ".ultrakb/config.yaml",
        kb_dir / ".env",
        config.GLOBAL_CONFIG_PATH,
        config.GLOBAL_CONFIG_DIR / ".env",
    )

    def read_versions() -> list[bytes | None]:
        return [path.read_bytes() if path.exists() else None for path in paths]

    with live_configuration(), config._with_global_config_lock(recover=False, **wait_options):
        for _ in range(3):
            before = read_versions()
            effective, sources = config.resolve_effective_config(kb_dir)
            config.validate_runtime_config(effective)
            raw = config.load_config(paths[0])
            config.validate_runtime_config(raw, allow_inherited=True)
            environment = dict(os.environ)
            for key, value in dotenv_values(paths[3]).items():
                if value and not environment.get(key):
                    environment[key] = value
            for key, value in dotenv_values(paths[1]).items():
                if value:
                    environment[key] = value
            bundle = config.resolve_credential_bundle(kb_dir)
            _, credential_sources = config.resolve_connection_credentials(
                kb_dir, model=effective["model"]
            )
            from ultrakb.model_tokenizer import tokenizer_asset

            asset = tokenizer_asset(effective)
            if read_versions() != before:
                continue
            if bundle.api_key:
                provider = str(effective["model"]).split("/")[0].upper()
                if "/" not in str(effective["model"]):
                    provider = "OPENAI"
                environment[f"{provider}_API_KEY"] = bundle.api_key
                environment.setdefault("OPENAI_API_KEY", bundle.api_key)
            from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info

            model_table = get_model_cost_map_source_info()
            if model_table["source"] == "local":
                environment["LITELLM_LOCAL_MODEL_COST_MAP"] = (
                    "true" if model_table["is_env_forced"] else "false"
                )
            return ConfigSnapshot(
                kb_dir=str(kb_dir),
                _payload=json.dumps(
                    {
                        "effective": effective,
                        "sources": sources,
                        "raw": raw,
                        "credentials": asdict(bundle),
                        "credential_sources": credential_sources,
                        "global_config_dir": str(config.GLOBAL_CONFIG_DIR),
                        "environment": environment,
                        "model_table": model_table,
                        "tokenizer_asset": asset,
                    },
                    ensure_ascii=False,
                ),
            )
    raise ValueError("Settings changed during capture; try starting the task again")
