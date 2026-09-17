"""Explicit stage controls shared by configuration, adapters and reuse contracts."""

from typing import Literal

from pydantic import BaseModel

Thinking = Literal["enabled", "disabled"]
Effort = Literal["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


class CompilationSettings(BaseModel):
    compilation_thinking: Thinking | None = None
    planning_thinking: Thinking | None = None
    verification_thinking: Thinking | None = None
    verification_adjudication_thinking: Thinking | None = None
    correction_thinking: Thinking | None = None
    compilation_reasoning_effort: Effort | None = None
    planning_reasoning_effort: Effort | None = None
    verification_reasoning_effort: Effort | None = None
    verification_adjudication_reasoning_effort: Effort | None = None
    correction_reasoning_effort: Effort | None = None


def setting_values(config):
    return {key: config.get(key) for key in CompilationSettings.model_fields}


def model_options(config, stage):
    if stage not in {
        "compilation",
        "planning",
        "verification",
        "verification_adjudication",
        "correction",
    }:
        raise ValueError("Invalid compilation stage")
    parent = "verification" if stage == "verification_adjudication" else "compilation"
    values = {}
    for suffix, allowed in (
        ("thinking", {"enabled", "disabled"}),
        ("reasoning_effort", {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}),
    ):
        # An absent control inherits; it never silently enables a provider mode.
        value = config.get(f"{stage}_{suffix}")
        if value is None and stage != "compilation":
            value = config.get(f"{parent}_{suffix}")
        if value is None and parent == "verification":
            value = config.get(f"compilation_{suffix}")
        if value is not None:
            if not isinstance(value, str) or value not in allowed:
                raise ValueError(f"Invalid {stage}_{suffix}")
            values[suffix] = {"type": value} if suffix == "thinking" else value
    if values.get("thinking") == {"type": "disabled"}:
        # Keep the configured effort available to enabled review/correction,
        # without contradicting an explicitly disabled request on the wire.
        values.pop("reasoning_effort", None)
    # The raw body preserves controls through adapters with older option enums.
    # Providers may reject unsupported controls; no provider mode is guessed here.
    return {"extra_body": values} if values else {}
