"""Validated model capacity contracts used by bounded processing."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from openkb.model_capabilities import capability_from_dict, selected_model_capabilities

DEFAULT_PROCESSING = {
    # This is a reservation, not a capability ceiling. The selected model and
    # endpoint supply capacity when callers have not set a concrete boundary.
    "output_tokens": 16384,
    "request_timeout": 180,
    "timeout_retries": 5,
    "stage_timeout": None,
    "document_timeout": None,
    "cleanup_timeout": 10,
    "max_attempts": 2,
    "max_requests": None,
    "max_tokens": None,
    "concurrency": 2,
}


class ProcessingIncomplete(BaseException):
    """A bounded operation is incomplete; its caller decides the recoverable scope."""

    def __init__(self, reason: str, stage: str = "compiling") -> None:
        super().__init__(reason)
        self.reason, self.stage = reason, stage


class OutputTruncated(ProcessingIncomplete):
    """A settled response that may be retried with more room or less evidence."""

    def __init__(self, stage: str) -> None:
        super().__init__("output_budget_exhausted", stage)


class InputTooLarge(ProcessingIncomplete):
    """A measured request that has not been sent and can be safely resized."""

    def __init__(self) -> None:
        super().__init__("input_budget_exceeded")


class ProviderContextExceeded(InputTooLarge):
    """A provider rejected capacity before completion; resize without replaying it."""

    def __init__(self, stage: str) -> None:
        ProcessingIncomplete.__init__(self, "provider_context_exceeded", stage)


def _capabilities(config: dict[str, Any]):
    """Discover a provider contract without treating a reservation as a ceiling."""

    if capabilities := capability_from_dict(config.get("_model_capabilities")):
        return capabilities
    model = config.get("model")
    return (
        selected_model_capabilities(model, config.get("_model_endpoint"))
        if isinstance(model, str) and model
        else None
    )


@dataclass(frozen=True)
class RequestLimits:
    context_tokens: int
    output_tokens: int
    request_timeout: float
    stage_timeout: float | None
    document_timeout: float | None
    cleanup_timeout: float
    max_attempts: int
    max_requests: int | None
    max_tokens: int | None
    concurrency: int
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None
    timeout_retries: int = 5
    input_tokens: int | None = None
    max_input_tokens: int | None = None
    shared_context: bool = True

    @property
    def input_capacity(self) -> int:
        return self.input_tokens or self.context_tokens - self.output_tokens

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> RequestLimits:
        values = config.get("processing")
        if not isinstance(values, dict):
            raise ProcessingIncomplete("execution_budget_required", "configuration")
        capabilities = _capabilities(config)
        output_value = values.get("output_tokens", DEFAULT_PROCESSING["output_tokens"])
        if type(output_value) is not int or output_value <= 0:
            raise ProcessingIncomplete("model_capabilities_required", "configuration")
        independent_contract = (
            "input_tokens" in values
            or "max_input_tokens" in values
            or values.get("shared_context") is False
        )
        if independent_contract:
            if values.get("shared_context", False) is not False:
                raise ProcessingIncomplete("model_capabilities_required", "configuration")
            input_tokens = values.get(
                "input_tokens", capabilities.input_tokens if capabilities else None
            )
            max_input_tokens = values.get("max_input_tokens", input_tokens)
            if capabilities:
                # A known shared envelope cannot safely be reinterpreted as
                # independent input/output capacity.  For a known independent
                # endpoint, every caller-selected input ceiling remains bound
                # by the endpoint's declared input maximum.
                capability_input = capabilities.input_tokens
                if (
                    capabilities.shared_context
                    or capability_input is None
                    or type(input_tokens) is not int
                    or type(max_input_tokens) is not int
                    or input_tokens > capability_input
                    or max_input_tokens > capability_input
                ):
                    raise ProcessingIncomplete("model_capabilities_required", "configuration")
            if "max_output_tokens" not in values and capabilities is None:
                # ``output_tokens`` is a per-request reservation, never an
                # unknown endpoint's independently safe completion ceiling.
                raise ProcessingIncomplete("model_capabilities_required", "configuration")
            output_ceiling = values.get(
                "max_output_tokens", capabilities.max_output_tokens if capabilities else None
            )
            if capabilities and (
                type(output_ceiling) is not int
                or output_ceiling <= 0
                or output_ceiling > capabilities.max_output_tokens
            ):
                raise ProcessingIncomplete("model_capabilities_required", "configuration")
            context = input_tokens
            output = (
                min(output_value, output_ceiling)
                if type(output_value) is int and type(output_ceiling) is int
                else output_value
            )
            shared_context = False
            ceilings = {
                "max_context_tokens": max_input_tokens,
                "max_output_tokens": output_ceiling,
            }
        elif capabilities:
            explicit_shared = (
                "context_tokens" in values
                or "max_context_tokens" in values
                or values.get("shared_context") is True
            )
            if explicit_shared:
                configured_context = values.get("context_tokens")
                configured_ceiling = values.get("max_context_tokens")
                max_context = (
                    configured_ceiling
                    if configured_ceiling is not None
                    else configured_context
                    if configured_context is not None
                    else capabilities.context_tokens
                )
                context = configured_context if configured_context is not None else max_context
                if (
                    type(context) is not int
                    or type(max_context) is not int
                    or context <= 0
                    or context > max_context
                    or max_context > capabilities.context_tokens
                ):
                    raise ProcessingIncomplete("model_capabilities_required", "configuration")
                input_tokens = max_input_tokens = None
                shared_context = True
            else:
                context = capabilities.context_tokens
                input_tokens = capabilities.input_tokens
                max_input_tokens = capabilities.input_tokens
                shared_context = capabilities.shared_context
                max_context = capabilities.context_tokens
            requested_output_ceiling = values.get(
                "max_output_tokens", capabilities.max_output_tokens
            )
            if (
                type(requested_output_ceiling) is not int
                or requested_output_ceiling <= 0
                or (
                    "max_output_tokens" in values
                    and requested_output_ceiling > capabilities.max_output_tokens
                )
            ):
                raise ProcessingIncomplete("model_capabilities_required", "configuration")
            output_ceiling = requested_output_ceiling
            output = (
                min(output_value, output_ceiling)
                if type(output_value) is int and type(output_ceiling) is int
                else output_value
            )
            ceilings = {
                "max_context_tokens": max_context,
                "max_output_tokens": output_ceiling,
            }
        else:
            if "context_tokens" not in values and "max_context_tokens" not in values:
                # No local model declaration means there is no safe generic
                # context capacity to invent.  An operator may explicitly
                # choose a shared envelope, or an independent I/O contract
                # above, but inherited output reservation alone is not one.
                raise ProcessingIncomplete("model_capabilities_required", "configuration")
            output_ceiling = values.get("max_output_tokens", output_value)
            configured_context = values.get("context_tokens")
            configured_ceiling = values.get("max_context_tokens")
            if "context_tokens" in values:
                context = configured_context
            else:
                context = configured_ceiling
            max_context = configured_ceiling if "max_context_tokens" in values else context
            output = (
                min(output_value, output_ceiling)
                if type(output_value) is int and type(output_ceiling) is int
                else output_value
            )
            input_tokens = max_input_tokens = None
            shared_context = True
            ceilings = {
                "max_context_tokens": max_context,
                "max_output_tokens": output_ceiling,
            }
        if (
            type(context) is not int
            or type(output) is not int
            or context <= 0
            or output <= 0
            or shared_context
            and output >= context
        ):
            raise ProcessingIncomplete("model_capabilities_required", "configuration")
        numbers: dict[str, Any] = {}
        retries = values.get("timeout_retries", DEFAULT_PROCESSING["timeout_retries"])
        if type(retries) is not int or retries < 0:
            raise ProcessingIncomplete("execution_budget_required", "configuration")
        numbers["timeout_retries"] = retries
        for key in ("request_timeout", "stage_timeout", "document_timeout", "cleanup_timeout"):
            value = values.get(key)
            if key in {"stage_timeout", "document_timeout"} and key in values and value is None:
                numbers[key] = None
            elif (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ProcessingIncomplete("execution_budget_required", "configuration")
            else:
                numbers[key] = value
        for key in ("max_attempts", "max_requests", "max_tokens", "concurrency"):
            value = values.get(key)
            if key in {"max_tokens", "max_requests"} and key in values and value is None:
                numbers[key] = None
            elif type(value) is not int or value <= 0:
                raise ProcessingIncomplete("execution_budget_required", "configuration")
            else:
                numbers[key] = value
        if any(
            type(value) is not int or value < initial
            for (key, initial), value in zip(
                (("max_context_tokens", context), ("max_output_tokens", output)), ceilings.values()
            )
        ):
            raise ProcessingIncomplete("model_capabilities_required", "configuration")
        if (
            shared_context
            and "max_output_tokens" in values
            and ceilings["max_output_tokens"] >= ceilings["max_context_tokens"]
        ):
            raise ProcessingIncomplete("model_capabilities_required", "configuration")
        if not shared_context and (
            type(input_tokens) is not int
            or type(max_input_tokens) is not int
            or input_tokens <= 0
            or max_input_tokens < input_tokens
        ):
            raise ProcessingIncomplete("model_capabilities_required", "configuration")
        numbers.update(ceilings)
        numbers.update(
            input_tokens=input_tokens,
            max_input_tokens=max_input_tokens,
            shared_context=shared_context,
        )
        numbers["concurrency"] = min(numbers["concurrency"], 4)
        return cls(context, output, **numbers)

    def expanded(self, *, reason: str | None = None) -> RequestLimits:
        """Grow only within the selected contract's explicit ceilings."""

        output_ceiling = self.max_output_tokens or self.output_tokens
        if self.shared_context:
            # Provider max-output is a capability ceiling, while this
            # operation may deliberately set a smaller shared context cap.
            # Never grow completion past the remaining one-token input slot.
            output_ceiling = min(
                output_ceiling, (self.max_context_tokens or self.context_tokens) - 1
            )

        return replace(
            self,
            context_tokens=min(
                self.context_tokens * 2, self.max_context_tokens or self.context_tokens
            ),
            input_tokens=(
                min(self.input_tokens * 2, self.max_input_tokens or self.input_tokens)
                if self.input_tokens is not None
                else None
            ),
            output_tokens=(
                self.output_tokens
                if reason == "input_budget_exceeded"
                else min(self.output_tokens * 2, output_ceiling)
            ),
        )

    def request(
        self, model: str, messages: list[dict], kwargs: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        import litellm

        from openkb.resource_budget import check_memory

        check_memory(
            sum(len(str(message.get("content", ""))) for message in messages) * 12
            + self.output_tokens * 32,
            stage="model_input",
        )
        try:
            tokens = litellm.token_counter(
                model=model, messages=messages, tools=kwargs.get("tools")
            )
        except Exception as exc:
            raise ProcessingIncomplete("input_budget_unknown") from exc
        output = kwargs.get("max_completion_tokens", kwargs.get("max_tokens", self.output_tokens))
        if type(output) is not int or output <= 0:
            raise ProcessingIncomplete("invalid_output_limit", "configuration")
        output = min(output, self.output_tokens)
        if kwargs.get("response_format"):
            import json

            tokens += litellm.token_counter(model=model, text=json.dumps(kwargs["response_format"]))
        if (
            tokens > self.input_capacity
            or self.shared_context
            and tokens + output > self.context_tokens
        ):
            raise InputTooLarge()
        options = dict(kwargs)
        options.pop("max_completion_tokens", None)
        return {
            **options,
            "max_tokens": output,
            "drop_params": False,
            "num_retries": 0,
            "max_retries": 0,
        }, tokens
