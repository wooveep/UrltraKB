"""Task-owned model bindings, atomic limits and completion execution.

Algorithms send content through a role fixed by their adapter. Only this module
turns the frozen credential bundle into transport options; none are persisted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator

import litellm

from openkb.config import LlmCredentialBundle
from openkb.llm_usage import UsageScope, active_scope
from openkb.llm_usage_transport import observe_model_call
from openkb.locks import LockCancelled

ROLES = frozenset(
    {
        "compile",
        "document_index",
        "conversation_index",
        "conversation_retrieval",
        "knowledge_retrieval",
        "answer",
    }
)
_GENERATION = frozenset(
    {
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "response_format",
        "stop",
        "seed",
        "parallel_tool_calls",
        "tool_choice",
        "reasoning_effort",
        "thinking",
    }
)


class ModelBudgetExceeded(RuntimeError):
    """No more requests may be sent during this execution."""


class ModelDeadlineExceeded(TimeoutError):
    """The task deadline expired; late model results cannot be published."""


@dataclass(frozen=True, init=False)
class RoleBindings:
    _payload: str = field(repr=False)

    def __init__(
        self,
        model: str,
        bundle: LlmCredentialBundle,
        *,
        conversation_model: str | None = None,
        retrieval_model: str | None = None,
        model_table: dict | None = None,
    ):
        models = {role: model for role in ROLES}
        if conversation_model is not None:
            models["conversation_index"] = conversation_model
        if retrieval_model is not None:
            models.update(
                conversation_retrieval=retrieval_model, knowledge_retrieval=retrieval_model
            )
        if any(not isinstance(value, str) or not value.strip() for value in models.values()):
            raise ValueError("Role models must be nonempty strings")
        if model_table is None:
            from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info

            model_table = get_model_cost_map_source_info()
        object.__setattr__(
            self,
            "_payload",
            json.dumps(
                {"models": models, "credentials": asdict(bundle), "model_table": model_table}
            ),
        )

    def model(self, role: str) -> str:
        if role not in ROLES:
            raise ValueError(f"Unknown model role: {role}")
        return json.loads(self._payload)["models"][role]

    def credentials(self) -> LlmCredentialBundle:
        return LlmCredentialBundle(**json.loads(self._payload)["credentials"])

    def public_identity(self) -> dict:
        values = json.loads(self._payload)
        return {
            "models": values["models"],
            "model_table": values["model_table"],
            "timeout": values["credentials"]["timeout"],
        }


@dataclass(frozen=True)
class ModelCallPolicy:
    max_calls: int = 1000
    concurrency: int = 5
    retries: int = 2
    backoff: float = 0.25
    deadline: float | None = None  # monotonic; shared across retries and roles
    cancelled: Callable[[], bool] = field(default=lambda: False, repr=False, compare=False)

    def __post_init__(self):
        for name, minimum in (("max_calls", 0), ("concurrency", 1), ("retries", 0)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if (
            type(self.backoff) not in (int, float)
            or not math.isfinite(self.backoff)
            or self.backoff < 0
        ):
            raise ValueError("backoff must be nonnegative")
        if self.deadline is not None and (
            type(self.deadline) not in (int, float) or not math.isfinite(self.deadline)
        ):
            raise ValueError("deadline must be finite")

    def check(self) -> None:
        if self.cancelled():
            raise LockCancelled("Model execution was stopped")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ModelDeadlineExceeded("Model execution deadline expired")

    def retryable(self, error: BaseException) -> bool:
        from litellm.exceptions import APIConnectionError, RateLimitError, Timeout

        return isinstance(error, (RateLimitError, Timeout, APIConnectionError)) or (
            isinstance(getattr(error, "status_code", None), int) and 500 <= error.status_code < 600  # type: ignore[attr-defined]
        )


@dataclass(frozen=True, init=False)
class ModelRequest:
    operation: str
    stage: str
    prompt_version: str
    parent_call_id: str | None
    _payload: str = field(repr=False)

    def __init__(
        self,
        messages: list[dict],
        *,
        operation: str,
        stage: str,
        prompt_version: str = "1",
        parent_call_id: str | None = None,
        tools: list[dict] | None = None,
        generation_options: dict | None = None,
        cache_segments: list[int] | None = None,
    ):
        options = dict(generation_options or {})
        if options.keys() - _GENERATION:
            raise ValueError(
                "Unpermitted generation options: " + ", ".join(sorted(options.keys() - _GENERATION))
            )
        if (
            not isinstance(messages, list)
            or not messages
            or any(
                not isinstance(m, dict)
                or m.get("role") not in {"system", "developer", "user", "assistant", "tool"}
                for m in messages
            )
        ):
            raise ValueError("Model messages must have valid roles")
        for name in ("max_tokens", "max_completion_tokens"):
            if name in options and (type(options[name]) is not int or options[name] <= 0):
                raise ValueError(f"Invalid generation option: {name}")
        for name, limit in (("temperature", 2), ("top_p", 1)):
            if name in options and (
                type(options[name]) not in (int, float) or not 0 <= options[name] <= limit
            ):
                raise ValueError(f"Invalid generation option: {name}")
        if "seed" in options and type(options["seed"]) is not int:
            raise ValueError("Invalid generation option: seed")
        if "parallel_tool_calls" in options and type(options["parallel_tool_calls"]) is not bool:
            raise ValueError("Invalid generation option: parallel_tool_calls")
        if "response_format" in options:
            value = options["response_format"]
            if not isinstance(value, dict) or value.get("type") not in {
                "text",
                "json_object",
                "json_schema",
            }:
                raise ValueError("Invalid generation option: response_format")
        if tools is not None and (
            not isinstance(tools, list)
            or any(
                not isinstance(tool, dict)
                or tool.get("type") != "function"
                or not isinstance(tool.get("function"), dict)
                or not isinstance(tool["function"].get("name"), str)
                for tool in tools
            )
        ):
            raise ValueError("Tools must be function definitions")
        segments = list(cache_segments or [])
        if any(type(i) is not int or i < 0 or i >= len(messages) for i in segments):
            raise ValueError("Cache segments must identify request messages")
        for name, value in (
            ("operation", operation),
            ("stage", stage),
            ("prompt_version", prompt_version),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be nonempty")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "parent_call_id", parent_call_id)
        object.__setattr__(
            self,
            "_payload",
            json.dumps(
                {
                    "messages": messages,
                    "tools": tools,
                    "options": options,
                    "cache_segments": segments,
                }
            ),
        )

    def payload(self) -> dict:
        return json.loads(self._payload)


@dataclass(frozen=True)
class CompletionResult:
    text: str
    tool_calls: tuple[Any, ...]
    finish_reason: str | None
    usage: Any
    raw_usage_available: bool
    request_id: str
    logical_call_id: str
    response: Any = field(repr=False, compare=False)


class CompletionExecutor:
    def __init__(
        self,
        bindings: RoleBindings,
        policy: ModelCallPolicy | None = None,
        *,
        scope: UsageScope | None = None,
    ):
        self.bindings = bindings
        self.policy = policy or ModelCallPolicy()
        self.scope = scope or active_scope()
        self._guard = threading.Lock()
        self._calls = 0
        self._slots = threading.BoundedSemaphore(self.policy.concurrency)
        identity = {
            **bindings.public_identity(),
            "policy": {
                name: getattr(self.policy, name)
                for name in ("max_calls", "concurrency", "retries", "backoff")
            },
        }
        self.fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @property
    def calls(self) -> int:
        with self._guard:
            return self._calls

    def reserve(self) -> None:
        self.policy.check()
        with self._guard:
            if self._calls >= self.policy.max_calls:
                raise ModelBudgetExceeded("Model request budget exhausted")
            self._calls += 1

    @asynccontextmanager
    async def slot(self):
        while not self._slots.acquire(blocking=False):
            self.policy.check()
            await asyncio.sleep(0.01)
        try:
            self.policy.check()
            yield
        finally:
            self._slots.release()

    @contextmanager
    def activate(self) -> Iterator[CompletionExecutor]:
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)

    def _kwargs(self, role: str, request: ModelRequest) -> dict:
        from openkb.llm_runtime import audit_step_headers

        self.policy.check()
        bundle = self.bindings.credentials()
        payload = request.payload()
        messages = payload["messages"]
        model = self.bindings.model(role)
        # Cache segments are provider hints, never application cache hits.
        if "anthropic" in model or "claude" in model:
            for i in payload["cache_segments"]:
                content = messages[i].get("content")
                if isinstance(content, str):
                    messages[i]["content"] = [
                        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                    ]
        kwargs = {
            "model": model,
            "messages": messages,
            "api_key": bundle.api_key,
            "base_url": bundle.base_url,
            "num_retries": 0,
            "max_retries": 0,
            **payload["options"],
        }
        if payload["tools"] is not None:
            kwargs["tools"] = payload["tools"]
        headers = audit_step_headers(bundle.extra_headers, request.stage)
        if headers:
            kwargs["extra_headers"] = headers
        timeout = bundle.timeout
        if self.policy.deadline is not None:
            remaining = self.policy.deadline - time.monotonic()
            timeout = min(timeout, remaining) if timeout is not None else remaining
        if timeout is not None:
            kwargs["timeout"] = timeout
        return kwargs

    @contextmanager
    def attempt(self, role: str, request: ModelRequest, logical_id: str):
        self.reserve()
        scope = active_scope() or self.scope
        if scope and self.scope and scope.kb_dir != self.scope.kb_dir:
            raise ValueError("Model execution belongs to another knowledge base")
        with observe_model_call(
            self.bindings.model(role),
            request.operation + "." + request.stage,
            scope=scope,
            logical_call_id=logical_id,
            parent_call_id=request.parent_call_id,
            operation=request.operation,
            prompt_version=request.prompt_version,
            fingerprint=self.fingerprint,
            before_send=self._before_send,
        ) as observed:
            yield observed

    def _before_send(self, count: int) -> None:
        self.policy.check()
        if count:
            self.reserve()  # Any additional SDK transport must use the same budget.

    def _result(self, response, observed, logical_id: str) -> CompletionResult:
        if observed is not None:
            observed.finish(response)
        self.policy.check()  # Retain spent usage, then discard a late result.
        choice = response.choices[0]
        content = choice.message.content or ""
        if isinstance(content, list):
            content = "\n".join(
                block.get("text", "") for block in content if block.get("type") == "text"
            )
        usage = getattr(response, "usage", None)
        return CompletionResult(
            content,
            tuple(getattr(choice.message, "tool_calls", None) or ()),
            getattr(choice, "finish_reason", None),
            usage,
            observed.raw_usage_available
            if observed and observed.raw_usage_available is not None
            else usage is not None,
            observed.first_id if observed else uuid.uuid4().hex,
            logical_id,
            response,
        )

    def _delay(self, retry: int) -> float:
        return min(self.policy.backoff * 2**retry, 10)

    def complete(self, role: str, request: ModelRequest) -> CompletionResult:
        self.policy.check()
        while not self._slots.acquire(timeout=0.05):
            self.policy.check()
        try:
            logical_id = uuid.uuid4().hex
            for retry in range(self.policy.retries + 1):
                self.policy.check()
                try:
                    with self.attempt(role, request, logical_id) as observed:
                        response = litellm.completion(**self._kwargs(role, request))
                        return self._result(response, observed, logical_id)
                except Exception as exc:
                    self.policy.check()
                    if (
                        retry == self.policy.retries
                        or not self.policy.retryable(exc)
                        or (observed is not None and not observed.sent_ids)
                    ):
                        raise
                until = time.monotonic() + self._delay(retry)
                while time.monotonic() < until:
                    self.policy.check()
                    time.sleep(min(0.05, max(0, until - time.monotonic())))
            raise AssertionError("Unreachable retry state")
        finally:
            self._slots.release()

    async def wait_response(self, awaitable):
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                self.policy.check()
                await asyncio.wait({task}, timeout=0.05)
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def acomplete(self, role: str, request: ModelRequest) -> CompletionResult:
        self.policy.check()
        while not self._slots.acquire(blocking=False):
            self.policy.check()
            await asyncio.sleep(0.01)
        try:
            logical_id = uuid.uuid4().hex
            for retry in range(self.policy.retries + 1):
                self.policy.check()
                try:
                    with self.attempt(role, request, logical_id) as observed:
                        response = await self.wait_response(
                            litellm.acompletion(**self._kwargs(role, request))
                        )
                        return self._result(response, observed, logical_id)
                except Exception as exc:
                    self.policy.check()
                    if (
                        retry == self.policy.retries
                        or not self.policy.retryable(exc)
                        or (observed is not None and not observed.sent_ids)
                    ):
                        raise
                until = time.monotonic() + self._delay(retry)
                while time.monotonic() < until:
                    self.policy.check()
                    await asyncio.sleep(min(0.05, max(0, until - time.monotonic())))
            raise AssertionError("Unreachable retry state")
        finally:
            self._slots.release()


_ACTIVE: ContextVar[CompletionExecutor | None] = ContextVar("openkb_model_executor", default=None)


def active_executor() -> CompletionExecutor | None:
    return _ACTIVE.get()


def check_model_stop() -> None:
    if executor := active_executor():
        executor.policy.check()


def raise_if_terminal(error: BaseException) -> None:
    import litellm

    if isinstance(
        error,
        (
            LockCancelled,
            asyncio.CancelledError,
            ModelBudgetExceeded,
            ModelDeadlineExceeded,
            litellm.AuthenticationError,
            litellm.BadRequestError,
        ),
    ):
        raise error


def executor_from_snapshot(snapshot, cancelled: Callable[[], bool]) -> CompletionExecutor:
    values = snapshot.values()
    config = values["effective"]
    options = validate_model_policy(config)
    deadline_seconds = options.pop("deadline_seconds", None)
    policy = ModelCallPolicy(
        **options,
        cancelled=cancelled,
        deadline=time.monotonic() + deadline_seconds if deadline_seconds is not None else None,
    )
    credentials = dict(values["credentials"])
    if credentials["api_key"] is None:
        provider = config["model"].split("/", 1)[0].upper() if "/" in config["model"] else "OPENAI"
        environment = values.get("environment", {})
        credentials["api_key"] = environment.get(provider + "_API_KEY") or environment.get(
            "OPENAI_API_KEY"
        )
    bindings = RoleBindings(
        config["model"],
        LlmCredentialBundle(**credentials),
        conversation_model=config.get("conversation_model"),
        retrieval_model=config.get("retrieval_model"),
        model_table=values.get("model_table"),
    )
    return CompletionExecutor(bindings, policy)


def validate_model_policy(config: dict) -> dict:
    """Validate the task configuration without capturing a deadline or credentials."""
    for name in ("conversation_model", "retrieval_model"):
        if config.get(name) is not None and (
            not isinstance(config[name], str) or not config[name].strip()
        ):
            raise ValueError(f"Configuration field '{name}' must be nonempty")
    raw = config.get("model_policy")
    allowed = {"max_calls", "concurrency", "retries", "backoff", "deadline_seconds"}
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


def compiler_executor(model: str, bundle, options: dict) -> CompletionExecutor:
    """Direct legacy calls capture once; application calls use their task binding."""
    if executor := active_executor():
        return executor
    from openkb.llm_runtime import get_extra_headers, get_timeout

    bundle = bundle or LlmCredentialBundle(extra_headers=get_extra_headers(), timeout=get_timeout())
    values = asdict(bundle)
    for name in ("api_key", "base_url", "timeout", "extra_headers"):
        if name in options:
            values[name] = options.pop(name)
    return CompletionExecutor(RoleBindings(model, LlmCredentialBundle(**values)))
