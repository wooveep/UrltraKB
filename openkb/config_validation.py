"""Validate local execution fields without changing provider passthrough options."""

import math
from pathlib import Path
from typing import Any


def validate_runtime_config(config: dict[str, Any], *, allow_inherited: bool = False) -> None:
    """Validate fields used directly by a local execution before it can start.

    Keep unknown provider options intact and leave the existing tolerant
    resolvers in charge of optional overrides. Errors name fields, never
    credential-bearing values from user configuration.
    """
    validate_model_policy(config)
    validate_role_generation(config.get("role_generation"))
    if config.get("index_navigation") is not None:
        navigation = config["index_navigation"]
        bounds = {"max_navigation_depth": 32, "max_nodes_per_unit": 32, "max_subtree_attempts": 3}
        if (
            not isinstance(navigation, dict)
            or navigation.keys() - bounds.keys()
            or any(
                type(value) is not int or not 1 <= value <= bounds[key]
                for key, value in navigation.items()
            )
        ):
            raise ValueError("Configuration field 'index_navigation' is invalid")
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


def validate_model_policy(config: dict) -> dict:
    """Validate the task configuration without capturing a deadline or credentials."""
    from openkb.llm_execution import ModelCallPolicy

    for name in ("conversation_model", "retrieval_model"):
        if config.get(name) is not None and (
            not isinstance(config[name], str) or not config[name].strip()
        ):
            raise ValueError(f"Configuration field '{name}' must be nonempty")
    raw = config.get("model_policy")
    allowed = {
        "max_calls",
        "concurrency",
        "retries",
        "backoff",
        "deadline_seconds",
        "retrieval_tokens",
        "retrieval_repair_tokens",
        "retrieval_repairs",
        "compile_reserve_calls",
        "compile_repair_calls",
    }
    if raw is not None and (not isinstance(raw, dict) or raw.keys() - allowed):
        raise ValueError("Configuration field 'model_policy' is invalid")
    options = dict(raw or {})
    seconds = options.pop("deadline_seconds", None)
    if seconds is not None and (
        type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0
    ):
        raise ValueError("model_policy.deadline_seconds must be finite and positive")
    ModelCallPolicy(**options)
    return {**options, "deadline_seconds": seconds}


def validate_role_generation(raw: dict | None) -> dict:
    """Capture opt-in role experiments without silently dropping provider options."""
    from openkb.llm_execution import ROLES, ModelRequest

    if raw is None:
        return {}
    if not isinstance(raw, dict) or raw.keys() - ROLES:
        raise ValueError("Configuration field role_generation has unknown roles")
    result = {}
    for role, options in raw.items():
        if not isinstance(options, dict) or options.keys() - {
            "reasoning_effort",
            "max_tokens",
            "temperature",
            "top_p",
        }:
            raise ValueError("Configuration field role_generation has unsupported options")
        if (
            role.endswith("retrieval")
            and "max_tokens" in options
            and options["max_tokens"] not in (1024, 2048, 4096)
        ):
            raise ValueError("role_generation retrieval max_tokens must be 1024, 2048 or 4096")
        try:
            request = ModelRequest(
                [{"role": "user", "content": "validation"}],
                operation=role,
                stage="validation",
                generation_options=options,
            )
        except ValueError as error:
            raise ValueError("Invalid role_generation option") from error
        result[role] = request.payload()["options"]
    return result
