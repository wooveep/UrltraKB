"""Document execution limits shared by compiler and navigation requests.

The execution owns request policy. SDK calls return data and never acquire
permission to commit knowledge. Configuration is captured before this scope.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from openkb.cancellation import check_cancelled


class ProcessingIncomplete(BaseException):
    """A bounded run ended without permission to publish partial knowledge."""

    def __init__(self, reason: str, stage: str = "compiling") -> None:
        super().__init__(reason)
        self.reason, self.stage = reason, stage


@dataclass(frozen=True)
class RequestLimits:
    context_tokens: int
    output_tokens: int
    request_timeout: float
    stage_timeout: float
    document_timeout: float
    cleanup_timeout: float
    max_attempts: int
    max_requests: int
    max_tokens: int
    concurrency: int

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> RequestLimits:
        values = config.get("processing")
        if not isinstance(values, dict):
            raise ProcessingIncomplete("execution_budget_required", "configuration")
        context = values.get("context_tokens")
        output = values.get("output_tokens")
        if type(context) is not int or type(output) is not int or not 0 < output < context:
            raise ProcessingIncomplete("model_capabilities_required", "configuration")
        numbers: dict[str, Any] = {}
        for key in ("request_timeout", "stage_timeout", "document_timeout", "cleanup_timeout"):
            value = values.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ProcessingIncomplete("execution_budget_required", "configuration")
            numbers[key] = value
        for key in ("max_attempts", "max_requests", "max_tokens", "concurrency"):
            value = values.get(key)
            if type(value) is not int or value <= 0:
                raise ProcessingIncomplete("execution_budget_required", "configuration")
            numbers[key] = value
        return cls(context, output, **numbers)

    def request(
        self, model: str, messages: list[dict], kwargs: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        import litellm

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
        if tokens + output > self.context_tokens:
            raise ProcessingIncomplete("input_budget_exceeded")
        # These parameters are execution invariants, not optional provider hints.
        options = dict(kwargs)
        options.pop("max_completion_tokens", None)
        return {
            **options,
            "max_tokens": output,
            "drop_params": False,
            "num_retries": 0,
            "max_retries": 0,
        }, tokens


@dataclass
class ExecutionBudget:
    limits: RequestLimits
    started: float = field(default_factory=time.monotonic)
    stage_started: float = field(default_factory=time.monotonic)
    stage: str = "converting"
    attempts: int = 0
    charged_tokens: int = 0
    unknown_usage: int = 0
    observations: list[dict[str, Any]] = field(default_factory=list)
    incomplete: ProcessingIncomplete | None = field(default=None, repr=False)
    lock: Any = field(default_factory=threading.RLock, repr=False)
    on_observation: Callable[[ExecutionBudget], None] = field(
        default=lambda value: None, repr=False
    )

    def __post_init__(self) -> None:
        self.permits = threading.BoundedSemaphore(self.limits.concurrency)

    def checkpoint(self, stage: str | None = None) -> float:
        if self.incomplete is not None:
            raise self.incomplete
        check_cancelled()
        now = time.monotonic()
        remaining = min(
            self.started + self.limits.document_timeout - now,
            self.stage_started + self.limits.stage_timeout - now,
        )
        if remaining <= 0:
            raise ProcessingIncomplete("time_budget_exhausted", self.stage)
        if stage and stage != self.stage:
            self.stage, self.stage_started = stage, now
        return remaining

    def reserve(self, kwargs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        remaining = self.checkpoint()
        options, tokens = self.limits.request(kwargs["model"], kwargs["messages"], kwargs)
        timeout = options.get("timeout")
        if timeout is None:
            timeout = self.limits.request_timeout
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ProcessingIncomplete("invalid_request_timeout", self.stage)
        options["timeout"] = min(timeout, remaining)
        reserved = tokens + options["max_tokens"]
        with self.lock:
            if self.attempts >= self.limits.max_requests:
                raise ProcessingIncomplete("request_budget_exhausted", self.stage)
            if self.charged_tokens + reserved > self.limits.max_tokens:
                raise ProcessingIncomplete("token_budget_exhausted", self.stage)
            self.attempts += 1
            self.charged_tokens += reserved
            observation = {
                "attempt": self.attempts,
                "stage": self.stage,
                "input_estimate": tokens,
                "output_reserve": options["max_tokens"],
                "timeout": options["timeout"],
                "reserved_tokens": reserved,
                "usage": None,
                "transport_attempts": None,
            }
            self.observations.append(observation)
            self.on_observation(self)
        return options, observation

    def settle(self, observation: dict[str, Any], response: Any) -> None:
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None)
        output_tokens = getattr(usage, "completion_tokens", None)
        with self.lock:
            if (
                type(input_tokens) is int
                and type(output_tokens) is int
                and min(input_tokens, output_tokens) >= 0
            ):
                self.charged_tokens += input_tokens + output_tokens - observation["reserved_tokens"]
                observation["usage"] = {"input": input_tokens, "output": output_tokens}
            self.on_observation(self)
        self.checkpoint()
        if getattr(response.choices[0], "finish_reason", None) == "length":
            raise ProcessingIncomplete("output_budget_exhausted", self.stage)
        if self.charged_tokens > self.limits.max_tokens:
            raise ProcessingIncomplete("token_budget_exhausted", self.stage)

    def call(self, function: Any, **kwargs: Any) -> Any:
        for attempt in range(self.limits.max_attempts):
            while not self.permits.acquire(timeout=0.05):
                self.checkpoint()
            observation = None
            try:
                options, observation = self.reserve(kwargs)
                # Only the transport runs here. It cannot publish Wiki changes.
                # SDK timeout commonly means idle-read time, so also bound the
                # elapsed request even when the provider keeps dripping bytes.
                done = threading.Event()
                values: list[Any] = []
                errors: list[BaseException] = []
                context = copy_context()

                def request() -> None:
                    try:
                        values.append(context.run(function, **options))
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        done.set()

                threading.Thread(target=request, name="openkb-budgeted-model", daemon=True).start()
                deadline = time.monotonic() + options["timeout"]
                while not done.wait(min(0.05, max(0, deadline - time.monotonic()))):
                    self.checkpoint()
                    if time.monotonic() >= deadline:
                        # Transport outcome is unknown. End this item; do not
                        # overlap another attempt with an outstanding request.
                        raise ProcessingIncomplete("request_timeout", self.stage)
                self.checkpoint()
                if time.monotonic() >= deadline:
                    raise ProcessingIncomplete("request_timeout", self.stage)
                if errors:
                    raise errors[0]
                response = values[0]
                self.settle(observation, response)
                return response
            except ProcessingIncomplete as exc:
                self.incomplete = exc
                raise
            except Exception as exc:
                if not _transient(exc) or attempt + 1 == self.limits.max_attempts:
                    raise
            finally:
                if observation is not None and observation["usage"] is None:
                    with self.lock:
                        self.unknown_usage += 1
                self.permits.release()
            deadline = time.monotonic() + min(0.25 * 2**attempt, self.checkpoint())
            while time.monotonic() < deadline:
                self.checkpoint()
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        raise AssertionError("Positive attempt limit required")

    async def acall(self, function: Any, **kwargs: Any) -> Any:
        for attempt in range(self.limits.max_attempts):
            while not self.permits.acquire(blocking=False):
                self.checkpoint()
                await asyncio.sleep(0.05)
            observation = None
            try:
                options, observation = self.reserve(kwargs)
                response = await asyncio.wait_for(function(**options), options["timeout"])
                self.settle(observation, response)
                return response
            except ProcessingIncomplete as exc:
                self.incomplete = exc
                raise
            except Exception as exc:
                if not _transient(exc) or attempt + 1 == self.limits.max_attempts:
                    raise
            finally:
                if observation is not None and observation["usage"] is None:
                    with self.lock:
                        self.unknown_usage += 1
                self.permits.release()
            await asyncio.sleep(min(0.25 * 2**attempt, self.checkpoint()))
        raise AssertionError("Positive attempt limit required")


def _transient(exc: Exception) -> bool:
    import litellm

    return isinstance(
        exc,
        (
            TimeoutError,
            litellm.Timeout,
            litellm.RateLimitError,
            litellm.ServiceUnavailableError,
            litellm.APIConnectionError,
        ),
    )


_ACTIVE: ContextVar[ExecutionBudget | None] = ContextVar("openkb_processing", default=None)


@contextmanager
def processing_scope(config: dict[str, Any]) -> Iterator[ExecutionBudget]:
    active = _ACTIVE.get()
    if active is not None:
        yield active
        return
    budget = ExecutionBudget(RequestLimits.from_config(config))
    token = _ACTIVE.set(budget)
    try:
        yield budget
    finally:
        from openkb.compilation_report import collect_compile_report

        with collect_compile_report() as report:
            report.usage.update(
                observable_attempts=budget.attempts,
                charged_tokens=budget.charged_tokens,
                unknown_usage=budget.unknown_usage,
                elapsed_seconds=time.monotonic() - budget.started,
                requests=budget.observations,
            )
        _ACTIVE.reset(token)


@contextmanager
def independent_processing_scope(config: dict[str, Any]) -> Iterator[ExecutionBudget]:
    """An optional post-publication operation owns a separate, finite allowance.

    It cannot consume or poison the necessary compilation's request allowance.
    Its caller records its usage separately instead of replacing the source report.
    """
    budget = ExecutionBudget(RequestLimits.from_config(config))
    token = _ACTIVE.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE.reset(token)


def cleanup_limit() -> float | None:
    active = _ACTIVE.get()
    return active.limits.cleanup_timeout if active else None


def model_call(function: Any, **kwargs: Any) -> Any:
    active = _ACTIVE.get()
    return active.call(function, **kwargs) if active else function(**kwargs)


async def model_acall(function: Any, **kwargs: Any) -> Any:
    active = _ACTIVE.get()
    return await active.acall(function, **kwargs) if active else await function(**kwargs)


def processing_checkpoint(stage: str | None = None) -> None:
    active = _ACTIVE.get()
    if active:
        active.checkpoint(stage)
    else:
        check_cancelled()


@contextmanager
def navigation_execution() -> Iterator[None]:
    import litellm
    from pageindex.execution import execution_scope

    from openkb.agent.compiler import _close_async_llm_clients

    def sync(**kwargs):
        return model_call(litellm.completion, **kwargs)

    async def asynchronous(**kwargs):
        return await model_acall(litellm.acompletion, **kwargs)

    with execution_scope(sync, asynchronous, processing_checkpoint, _close_async_llm_clients):
        yield


def validate_usage(value: Any) -> None:
    """Validate persisted observations before they enter task presentation."""
    if not isinstance(value, dict):
        raise ValueError("Invalid execution usage")
    if not value:
        return
    if set(value) != {
        "observable_attempts",
        "charged_tokens",
        "unknown_usage",
        "elapsed_seconds",
        "requests",
    }:
        raise ValueError("Invalid execution usage fields")
    for key in ("observable_attempts", "charged_tokens", "unknown_usage"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("Invalid execution usage count")
    elapsed = value["elapsed_seconds"]
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise ValueError("Invalid execution duration")
    if not isinstance(value["requests"], list):
        raise ValueError("Invalid request observations")
    for row in value["requests"]:
        if not isinstance(row, dict) or set(row) != {
            "attempt",
            "stage",
            "input_estimate",
            "output_reserve",
            "timeout",
            "reserved_tokens",
            "usage",
            "transport_attempts",
        }:
            raise ValueError("Invalid request observation")
        if not isinstance(row["stage"], str) or row["transport_attempts"] is not None:
            raise ValueError("Invalid request observation details")
        for key in ("attempt", "input_estimate", "output_reserve", "reserved_tokens"):
            if type(row[key]) is not int or row[key] < 0:
                raise ValueError("Invalid request token count")
        timeout = row["timeout"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Invalid request timeout observation")
        usage = row["usage"]
        if usage is not None and (
            not isinstance(usage, dict)
            or set(usage) != {"input", "output"}
            or any(type(count) is not int or count < 0 for count in usage.values())
        ):
            raise ValueError("Invalid model usage observation")
