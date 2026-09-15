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
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterator

from openkb.cancellation import check_cancelled
from openkb.execution_allowance import active_allowance
from openkb.execution_measurement import Measurement, measurement_scope, validate_measurement

# Request capacities are configurable model ceilings, not provider discovery.
DEFAULT_PROCESSING = {
    "context_tokens": 262144,
    "output_tokens": 131072,
    "max_context_tokens": 1048576,
    "max_output_tokens": 393216,
    "request_timeout": 180,
    "timeout_retries": 5,
    "stage_timeout": None,
    "document_timeout": None,
    "cleanup_timeout": 10,
    "max_attempts": 2,
    "max_requests": None,
    "max_tokens": None,
    "concurrency": 8,
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
        retries = values.get("timeout_retries", DEFAULT_PROCESSING["timeout_retries"])
        if type(retries) is not int or retries < 0:
            raise ProcessingIncomplete("execution_budget_required", "configuration")
        numbers["timeout_retries"] = retries
        for key in ("request_timeout", "stage_timeout", "document_timeout", "cleanup_timeout"):
            value = values.get(key)
            if key in {"stage_timeout", "document_timeout"} and key in values and value is None:
                numbers[key] = None
                continue
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
            if key in {"max_tokens", "max_requests"} and key in values and value is None:
                numbers[key] = None
                continue
            if type(value) is not int or value <= 0:
                raise ProcessingIncomplete("execution_budget_required", "configuration")
            numbers[key] = value
        for key, initial in (("max_context_tokens", context), ("max_output_tokens", output)):
            value = values.get(key, initial)
            if type(value) is not int or value < initial:
                raise ProcessingIncomplete("model_capabilities_required", "configuration")
            numbers[key] = value
        if numbers["max_output_tokens"] >= numbers["max_context_tokens"]:
            raise ProcessingIncomplete("model_capabilities_required", "configuration")
        return cls(context, output, **numbers)

    def expanded(self, *, reason: str | None = None) -> RequestLimits:
        """Grow within explicit ceilings without enlarging an already sufficient output."""
        return replace(
            self,
            context_tokens=min(
                self.context_tokens * 2, self.max_context_tokens or self.context_tokens
            ),
            output_tokens=(
                self.output_tokens
                if reason == "input_budget_exceeded"
                else min(self.output_tokens * 2, self.max_output_tokens or self.output_tokens)
            ),
        )

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
            raise InputTooLarge()
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
    measurement: Measurement = field(default_factory=Measurement, repr=False)
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
            (
                start + limit - now
                for start, limit in (
                    (self.started, self.limits.document_timeout),
                    (self.stage_started, self.limits.stage_timeout),
                )
                if limit is not None
            ),
            default=math.inf,
        )
        if remaining <= 0:
            raise ProcessingIncomplete("time_budget_exhausted", self.stage)
        if allowance := active_allowance():
            remaining = min(remaining, allowance.remaining())
        if stage and stage != self.stage:
            self.stage, self.stage_started = stage, now
        return remaining

    def reserve(
        self, kwargs: dict[str, Any], limits: RequestLimits | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.checkpoint()
        options, tokens = (limits or self.limits).request(
            kwargs["model"], kwargs["messages"], kwargs
        )
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
        reserved = tokens + options["max_tokens"]
        with self.lock:
            # Token counting and adapter preparation may overlap a remote
            # failure. Admission and the stop latch share the same lock.
            options["timeout"] = min(timeout, self.checkpoint())
            if self.limits.max_requests is not None and self.attempts >= self.limits.max_requests:
                raise ProcessingIncomplete("request_budget_exhausted", self.stage)
            if (
                self.limits.max_tokens is not None
                and self.charged_tokens + reserved > self.limits.max_tokens
            ):
                raise ProcessingIncomplete("token_budget_exhausted", self.stage)
            if allowance := active_allowance():
                allowance.before_reserve(options, reserved)
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
        if self.limits.max_tokens is not None and self.charged_tokens > self.limits.max_tokens:
            raise ProcessingIncomplete("token_budget_exhausted", self.stage)
        if getattr(response.choices[0], "finish_reason", None) == "length":
            raise OutputTruncated(self.stage)

    def call(self, function: Any, **kwargs: Any) -> Any:
        while True:
            limits = self.limits
            try:
                return self._call(function, limits, **kwargs)
            except (OutputTruncated, InputTooLarge) as exc:
                if not self.expand(limits, kwargs, exc.reason):
                    raise

    def expand(self, previous: RequestLimits, kwargs: dict[str, Any], reason: str) -> bool:
        # Explicit per-operation output limits (e.g. navigation) remain binding.
        if "max_tokens" in kwargs or "max_completion_tokens" in kwargs:
            return False
        if (
            reason == "output_budget_exhausted"
            and previous.output_tokens == previous.max_output_tokens
        ):
            return False
        if (
            reason == "input_budget_exceeded"
            and previous.context_tokens == previous.max_context_tokens
        ):
            return False
        with self.lock:
            self.checkpoint()
            expanded = previous.expanded(reason=reason)
            if expanded == previous:
                return False
            if self.limits == previous:
                self.limits = expanded
            from openkb.log import logger

            logger.info(
                "%s [%s]; retrying with context=%s, output=%s",
                reason,
                self.stage,
                self.limits.context_tokens,
                self.limits.output_tokens,
            )
            return True

    def _call(self, function: Any, limits: RequestLimits, **kwargs: Any) -> Any:
        from openkb.model_retry import StreamTimeoutRetries
        from openkb.provider_usage import WireUsage

        retries = StreamTimeoutRetries(self)
        attempt = 0
        while True:
            waiting = time.monotonic()
            while not self.permits.acquire(timeout=0.05):
                self._check_queue(waiting)
            acquired = time.monotonic()
            observation = None
            measured = None
            done = None
            activity = None
            try:
                options, observation = self.reserve(kwargs, limits)
                wire_usage = WireUsage(options)
                measured = self.measurement.begin_request(
                    observation, acquired - waiting, time.monotonic() - acquired, options=options
                )
                # Streamed compiler calls measure silence between meaningful
                # deltas. Other calls retain their elapsed request deadline.
                from openkb.model_stream import StreamActivity, collect

                activity = StreamActivity() if options.get("stream") else None
                done = threading.Event()
                values: list[Any] = []
                errors: list[BaseException] = []
                context = copy_context()

                def request() -> None:
                    try:
                        values.append(
                            context.run(collect, function, options, activity)
                            if activity is not None
                            else context.run(function, **options)
                        )
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        self.measurement.finish_request(measured)
                        done.set()

                threading.Thread(target=request, name="openkb-budgeted-model", daemon=True).start()
                deadline = time.monotonic() + options["timeout"]
                while not done.wait(min(0.05, max(0, deadline - time.monotonic()))):
                    self.checkpoint()
                    if activity is not None:
                        measured["response_activity"] = activity.snapshot()
                        deadline = time.monotonic() + max(0, activity.remaining(options["timeout"]))
                    if time.monotonic() >= deadline:
                        # A retry must first finish this local stream transport.
                        if allowance := active_allowance():
                            allowance.request_timed_out(options)
                        raise ProcessingIncomplete("request_timeout", self.stage)
                self.checkpoint()
                if activity is None and time.monotonic() >= deadline:
                    if allowance := active_allowance():
                        allowance.request_timed_out(options)
                    raise ProcessingIncomplete("request_timeout", self.stage)
                if errors:
                    raise errors[0]
                response = wire_usage.restore(values[0])
                self.measurement.provider_usage(
                    measured, getattr(response, "usage", None), response=response
                )
                self.settle(observation, response)
                from openkb.execution_receipt import dispatched_output

                dispatched_output(options)
                return response
            except (OutputTruncated, InputTooLarge):
                raise  # Settled or never sent: the compiler can shrink the batch.
            except ProcessingIncomplete as exc:
                if retries.retry(exc, activity, done):
                    continue
                with self.lock:
                    self.incomplete = exc
                raise
            except Exception as exc:
                if _uncertain_transport(exc):
                    if retries.retry(exc, activity, done):
                        continue
                    with self.lock:
                        self.incomplete = ProcessingIncomplete(
                            "request_timeout"
                            if activity is not None and retries.is_timeout(exc)
                            else "request_outcome_unknown",
                            self.stage,
                        )
                    raise self.incomplete from None
                if not _transient(exc):
                    raise
                if attempt + 1 == self.limits.max_attempts:
                    raise ProcessingIncomplete(
                        "provider_temporarily_unavailable", self.stage
                    ) from None
            finally:
                if measured is not None and activity is not None:
                    measured["response_activity"] = activity.snapshot()
                if measured is not None and done is not None and not done.is_set():
                    self.measurement.observe_pending(measured)
                if observation is not None and observation["usage"] is None:
                    with self.lock:
                        self.unknown_usage += 1
                if done is not None and not done.is_set():
                    # A degraded optional request may still be on the wire. Keep
                    # its global concurrency slot until that transport settles.
                    def release_when_done(event=done):
                        event.wait()
                        self.permits.release()

                    threading.Thread(target=release_when_done, daemon=True).start()
                else:
                    self.permits.release()
            deadline = time.monotonic() + min(0.25 * 2**attempt, self.checkpoint())
            while time.monotonic() < deadline:
                self.checkpoint()
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            attempt += 1

    def _check_queue(self, waiting):
        self.checkpoint()
        # A timed-out optional transport still owns its slot. Even profiles
        # without stage/document ceilings must eventually stop waiting for it.
        if time.monotonic() - waiting >= self.limits.request_timeout:
            self.incomplete = ProcessingIncomplete("request_queue_timeout", self.stage)
            raise self.incomplete

    async def acall(self, function: Any, **kwargs: Any) -> Any:
        while True:
            limits = self.limits
            try:
                return await self._acall(function, limits, **kwargs)
            except (OutputTruncated, InputTooLarge) as exc:
                if not self.expand(limits, kwargs, exc.reason):
                    raise

    async def _acall(self, function: Any, limits: RequestLimits, **kwargs: Any) -> Any:
        from openkb.provider_usage import WireUsage

        for attempt in range(self.limits.max_attempts):
            waiting = time.monotonic()
            while not self.permits.acquire(blocking=False):
                self._check_queue(waiting)
                await asyncio.sleep(0.05)
            acquired = time.monotonic()
            observation = None
            measured = None
            try:
                options, observation = self.reserve(kwargs, limits)
                wire_usage = WireUsage(options)
                measured = self.measurement.begin_request(
                    observation, acquired - waiting, time.monotonic() - acquired, options=options
                )
                response = await asyncio.wait_for(function(**options), options["timeout"])
                wire_usage.restore(response)
                self.measurement.provider_usage(
                    measured, getattr(response, "usage", None), response=response
                )
                self.settle(observation, response)
                from openkb.execution_receipt import dispatched_output

                dispatched_output(options)
                return response
            except (OutputTruncated, InputTooLarge):
                raise
            except ProcessingIncomplete as exc:
                with self.lock:
                    self.incomplete = exc
                raise
            except Exception as exc:
                if _uncertain_transport(exc):
                    with self.lock:
                        self.incomplete = ProcessingIncomplete(
                            "request_outcome_unknown", self.stage
                        )
                    raise self.incomplete from None
                if not _transient(exc):
                    raise
                if attempt + 1 == self.limits.max_attempts:
                    raise ProcessingIncomplete(
                        "provider_temporarily_unavailable", self.stage
                    ) from None
            finally:
                if measured is not None:
                    self.measurement.finish_request(measured)
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
            litellm.RateLimitError,
            litellm.ServiceUnavailableError,
        ),
    )


def _uncertain_transport(exc: Exception) -> bool:
    import litellm

    return isinstance(
        exc, (TimeoutError, ConnectionError, litellm.Timeout, litellm.APIConnectionError)
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
        with measurement_scope(budget.measurement):
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
                measurement=budget.measurement.value,
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
    # Compiler operations already declare their request identity. Streaming is
    # a transport detail and does not invalidate completed semantic checkpoints.
    import litellm

    from openkb.execution_measurement import current_operation
    from openkb.execution_receipt import dispatched_output

    if current_operation() and function is litellm.completion:
        kwargs.setdefault("stream", True)
        kwargs.setdefault("stream_options", {"include_usage": True})
    dispatched_output(kwargs)
    active = _ACTIVE.get()
    if active:
        return active.call(function, **kwargs)
    if kwargs.get("stream"):
        from openkb.model_stream import StreamActivity, collect

        return collect(function, kwargs, StreamActivity())
    return function(**kwargs)


async def model_acall(function: Any, **kwargs: Any) -> Any:
    from openkb.execution_receipt import dispatched_output

    dispatched_output(kwargs)
    active = _ACTIVE.get()
    return await active.acall(function, **kwargs) if active else await function(**kwargs)


def processing_checkpoint(stage: str | None = None) -> None:
    active = _ACTIVE.get()
    if active:
        active.checkpoint(stage)
    else:
        check_cancelled()


def processing_wait_limit() -> float:
    active = _ACTIVE.get()
    return active.limits.request_timeout if active else 180.0


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
    if set(value) - {"measurement"} != {
        "observable_attempts",
        "charged_tokens",
        "unknown_usage",
        "elapsed_seconds",
        "requests",
    }:
        raise ValueError("Invalid execution usage fields")
    if "measurement" in value:
        validate_measurement(value["measurement"])
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
            or set(usage) not in ({"input", "output"}, {"total"})
            or any(type(count) is not int or count < 0 for count in usage.values())
        ):
            raise ValueError("Invalid model usage observation")


@contextmanager
def external_request_usage(reservation: int, stage: str = "external"):
    """Reserve and settle a secondary protocol request inside the active task budget."""
    active = _ACTIVE.get()
    receipt: dict[str, Any] = {}
    if active is None:
        yield receipt
        return
    active.checkpoint()
    with active.lock:
        if active.limits.max_requests is not None and active.attempts >= active.limits.max_requests:
            active.incomplete = ProcessingIncomplete("request_budget_exhausted", active.stage)
            raise active.incomplete
        if (
            active.limits.max_tokens is not None
            and active.charged_tokens + reservation > active.limits.max_tokens
        ):
            active.incomplete = ProcessingIncomplete("token_budget_exhausted", active.stage)
            raise active.incomplete
        active.attempts += 1
        active.charged_tokens += reservation
        observation = {
            "attempt": active.attempts,
            "stage": stage,
            "reserved_tokens": reservation,
            "usage": None,
            "transport_attempts": None,
            "input_estimate": reservation,
            "output_reserve": 0,
            "timeout": min(active.checkpoint(), active.limits.request_timeout),
        }
        active.observations.append(observation)
        active.on_observation(active)
    measured = active.measurement.begin_request(observation, 0.0, 0.0)
    try:
        yield receipt
    finally:
        active.measurement.finish_request(measured)
        for field in ("cache_read_tokens", "cache_write_tokens"):
            value = receipt.get(field)
            if type(value) is int and value >= 0:
                measured[field] = value
        with active.lock:
            tokens = receipt.get("tokens")
            if type(tokens) is int and tokens >= 0:
                active.charged_tokens += tokens - reservation
                observation["usage"] = (
                    {"input": receipt["input"], "output": receipt["output"]}
                    if type(receipt.get("input")) is int and type(receipt.get("output")) is int
                    else {"total": tokens}
                )
            else:
                active.unknown_usage += 1
            active.on_observation(active)
        processing_checkpoint()
        if (
            active.limits.max_tokens is not None
            and active.charged_tokens > active.limits.max_tokens
        ):
            active.incomplete = ProcessingIncomplete("token_budget_exhausted", active.stage)
            raise active.incomplete


def request_budget_settings() -> dict[str, Any] | None:
    """Read per-request caps without exposing the mutable task budget."""
    active = _ACTIVE.get()
    if active is None:
        return None
    return {
        "max_tokens": active.limits.output_tokens,
        "timeout": min(active.checkpoint(), active.limits.request_timeout),
    }
