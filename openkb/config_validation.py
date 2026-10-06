"""Validate local execution fields without changing provider passthrough options."""

from pathlib import Path
from typing import Any


def validate_runtime_config(config: dict[str, Any], *, allow_inherited: bool = False) -> None:
    """Validate fields used directly by a local execution before it can start.

    Keep unknown provider options intact and leave the existing tolerant
    resolvers in charge of optional overrides. Errors name fields, never
    credential-bearing values from user configuration.
    """
    from openkb.llm_execution import validate_model_policy

    validate_model_policy(config)
    if config.get("extraction_budget") is not None:
        from openkb.pending.records import ExecutionBudget

        ExecutionBudget.model_validate(config["extraction_budget"])
    runtime = config.get("office_runtime_path")
    if runtime is not None and (not isinstance(runtime, str) or not Path(runtime).is_absolute()):
        raise ValueError("Configuration field 'office_runtime_path' must be an absolute path")
    timeout = config.get("office_timeout_seconds")
    if timeout is not None and (type(timeout) is not int or not 1 <= timeout <= 3600):
        raise ValueError("Configuration field 'office_timeout_seconds' must be between 1 and 3600")
    if (
        config.get("download_remote_assets") is not None
        and type(config["download_remote_assets"]) is not bool
    ):
        raise ValueError("Configuration field 'download_remote_assets' must be a boolean")
    for key in ("model", "language"):
        value = config.get(key)
        if allow_inherited and value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"Configuration field '{key}' must be a string")
    for key in ("pageindex_threshold", "pdf_short_max_pages"):
        value = config.get(key)
        if value is None and (allow_inherited or key not in config):
            continue
        if type(value) is not int:
            raise ValueError(f"Configuration field '{key}' must be an integer")
        if key == "pdf_short_max_pages" and value < 0:
            raise ValueError("Configuration field 'pdf_short_max_pages' must be nonnegative")
    if config.get("model_capacity") is not None:
        from openkb.processing_policy import ModelCapacity

        try:
            ModelCapacity.model_validate(config["model_capacity"])
        except ValueError:
            raise ValueError("Configuration field 'model_capacity' is invalid") from None
