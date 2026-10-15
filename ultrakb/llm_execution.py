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
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Iterator, Mapping

import litellm

from ultrakb.config import LlmCredentialBundle
from ultrakb.llm_usage import UsageScope, active_scope
from ultrakb.llm_usage_transport import observe_model_call
from ultrakb.locks import LockCancelled
from ultrakb.model_tokenizer import ContextCapacityError, TokenMeasure

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
# These SDKs use device login, workload credentials, or an unauthenticated
# local daemon instead of the task's API-key bundle.
_SDK_AUTH_PROVIDERS = frozenset(
    {
        "chatgpt",
        "github_copilot",
        "bedrock",
        "bedrock_converse",
        "vertex_ai",
        "vertex_ai_beta",
        "sagemaker",
        "ollama",
        "ollama_chat",
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
        "drop_params",
    }
)


class StageBudgetExceeded(RuntimeError):
    """A bounded indexing/retrieval stage stops; the answer may use remaining task sends."""


class AnswerReserveReached(StageBudgetExceeded):
    """Only the protected final-answer send remains; ordinary tools must stop."""


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
        generation: dict | None = None,
    ):
        from ultrakb.config_validation import validate_role_generation

        generation = validate_role_generation(generation)
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
                {
                    "models": models,
                    "credentials": asdict(bundle),
                    "model_table": model_table,
                    "generation": generation,
                }
            ),
        )

    def model(self, role: str) -> str:
        if role not in ROLES:
            raise ValueError(f"Unknown model role: {role}")
        return json.loads(self._payload)["models"][role]

    def generation_options(self, role: str) -> dict:
        self.model(role)
        return json.loads(self._payload)["generation"].get(role, {})

    def credentials(self) -> LlmCredentialBundle:
        return LlmCredentialBundle(**json.loads(self._payload)["credentials"])

    def validate_request_credentials(
        self, role: str, headers: Mapping[str, str], *, selected_auth: str | None = None
    ) -> None:
        """Reject implicit API-key fallback at the actual model-send boundary."""
        bundle = self.credentials()
        model = self.model(role)
        provider = model.split("/", 1)[0] if "/" in model else "openai"
        sdk_auth = provider in _SDK_AUTH_PROVIDERS
        # Azure also supports Entra/workload tokens. An API-key header here
        # would instead come from an uncaptured SDK/environment fallback.
        if provider in {"azure", "azure_ai"}:
            sdk_auth = "api-key" not in headers
        elif provider in {"ollama", "ollama_chat"}:
            captured_headers = {key.lower(): value for key, value in bundle.extra_headers.items()}
            sdk_auth = not headers.get("authorization") or headers.get("authorization") == (
                captured_headers.get("authorization")
            )
        if selected_auth is not None:
            sdk_auth = selected_auth == "sdk" and "api-key" not in headers
        if not bundle.api_key and not sdk_auth:
            raise litellm.AuthenticationError(
                message=(
                    "No API key was captured for this model task; "
                    "configure credentials and start a new task"
                ),
                model=model,
                llm_provider=provider,
            )

    def public_identity(self) -> dict:
        values = json.loads(self._payload)
        return {
            "models": values["models"],
            "model_table": values["model_table"],
            "role_generation": values["generation"],
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
    retrieval_tokens: int = 1024
    retrieval_repair_tokens: int = 8192
    retrieval_repairs: int = 2
    compile_reserve_calls: int = 3  # Overview, plan and one bounded plan correction.
    compile_repair_calls: int = 1

    def __post_init__(self):
        if type(self.retrieval_tokens) is not int or self.retrieval_tokens not in (
            1024,
            2048,
            4096,
        ):
            raise ValueError("retrieval_tokens must be 1024, 2048 or 4096")
        if type(self.retrieval_repair_tokens) is not int or not (
            self.retrieval_tokens <= self.retrieval_repair_tokens <= 8192
        ):
            raise ValueError("retrieval_repair_tokens must be within initial tokens and 8192")
        if type(self.retrieval_repairs) is not int or not 0 <= self.retrieval_repairs <= 2:
            raise ValueError("retrieval_repairs must be between 0 and 2 per question")
        for name, minimum in (
            ("max_calls", 0),
            ("concurrency", 1),
            ("retries", 0),
            ("compile_reserve_calls", 2),
            ("compile_repair_calls", 0),
        ):
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
        if {"max_tokens", "max_completion_tokens"} <= options.keys():
            raise ValueError("Specify only one output token limit")
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
        if "reasoning_effort" in options and (
            not isinstance(options["reasoning_effort"], str)
            or options["reasoning_effort"]
            not in {"none", "minimal", "low", "medium", "high", "xhigh"}
        ):
            raise ValueError("Invalid generation option: reasoning_effort")
        for name in ("parallel_tool_calls", "drop_params"):
            if name in options and type(options[name]) is not bool:
                raise ValueError(f"Invalid generation option: {name}")
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
        self.model_errors: list[BaseException] = []
        base_policy = policy or ModelCallPolicy()
        self._stop_requested = threading.Event()
        self.policy = replace(
            base_policy,
            cancelled=lambda: (self._stop_requested.is_set() or base_policy.cancelled()),
        )
        self.scope = scope or active_scope()
        self._guard = threading.Lock()
        from ultrakb.execution_budget import TaskBudget
        from ultrakb.image_delivery import ImageDeliveries

        self.budget = TaskBudget(self.policy)
        self.image_deliveries = ImageDeliveries()
        self.request_cache: dict[str, Any] = {}
        self.question_capacity: TokenMeasure | None = None
        self.compile_capacity: TokenMeasure | None = None
        self.index_capacity_policy: dict | None = None
        self._answer_input_tokens = 0
        self._tool_output_tokens = 0
        self._retrieval_slots = threading.BoundedSemaphore(1)
        self._slots = threading.BoundedSemaphore(self.policy.concurrency)
        identity = {
            **bindings.public_identity(),
            "policy": {
                name: getattr(self.policy, name)
                for name in (
                    "max_calls",
                    "concurrency",
                    "retries",
                    "backoff",
                    "retrieval_tokens",
                    "retrieval_repair_tokens",
                    "retrieval_repairs",
                    "compile_reserve_calls",
                    "compile_repair_calls",
                )
            },
        }
        self.fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @property
    def calls(self) -> int:
        return self.budget.snapshot()["used"]

    def begin_question(self) -> None:
        """Renew stage allowances while preserving the task's total budget and deadline."""
        self.policy.check()
        from ultrakb.image_delivery import ImageDeliveries

        with self._guard:
            self.budget.reset_question()
            self.request_cache.clear()
            self.image_deliveries = ImageDeliveries()
            self.question_capacity = None
            self._answer_input_tokens = self._tool_output_tokens = 0

    def cancel(self) -> None:
        self._stop_requested.set()

    def check_capacity_input(self, payload, *, role="answer") -> None:
        from ultrakb.llm_images import require_text_input

        require_text_input(payload)
        measure = self.question_capacity
        tokens = measure.payload_tokens(payload) if measure is not None else 0
        if role == "answer":
            with self._guard:
                self._answer_input_tokens, self._tool_output_tokens = tokens, 0
        if measure is not None and tokens > measure.input_limit:
            message = (
                "Prepared model input exceeds the captured capacity; current input is retained"
            )
            if role == "answer":
                raise ContextCapacityError(message)
            raise ValueError(message)  # An index/search may retain limited coverage.

    def reserve_tool_output(self, payload, *, minimum_tokens: int) -> bool:
        """All outputs in one tool turn share the remaining answer input capacity."""
        measure = self.question_capacity
        if measure is None:
            return True
        tokens = max(minimum_tokens, measure.payload_tokens(payload) + 512)
        with self._guard:
            if self._answer_input_tokens + self._tool_output_tokens + tokens > measure.input_limit:
                return False
            self._tool_output_tokens += tokens
        return True

    @property
    def stage_calls(self) -> dict[str, int]:
        return self.budget.snapshot()["stages"]

    @contextmanager
    def retrieval_slot(self):
        while not self._retrieval_slots.acquire(blocking=False):
            self.policy.check()
            time.sleep(0.01)
        try:
            self.policy.check()
            yield
        finally:
            self._retrieval_slots.release()

    def reserve(self, role: str = "answer") -> None:
        self.budget.sent(self.budget.acquire(role))

    def allow_retrieval_repair(self) -> bool:
        """Reserve one logical repair; actual sends still use reserve/transport accounting."""
        self.policy.check()
        with self._guard:
            if self.calls >= self.policy.max_calls:
                raise ModelBudgetExceeded("Model request budget exhausted")
            repairs = self.request_cache.get("retrieval_repairs", 0)
            if (
                self.stage_calls.get("retrieval", 0) >= 8
                or repairs >= self.policy.retrieval_repairs
            ):
                return False
            self.request_cache["retrieval_repairs"] = repairs + 1
            return True

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
        from ultrakb.llm_request_options import generation_kwargs
        from ultrakb.llm_runtime import audit_step_headers

        self.policy.check()
        bundle = self.bindings.credentials()
        payload = request.payload()
        messages = payload["messages"]
        from ultrakb.llm_images import require_text_input

        require_text_input(messages)
        model = self.bindings.model(role)
        captured_options = self.bindings.generation_options(role)
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
            **generation_kwargs(model, bundle.base_url, captured_options, payload["options"]),
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
        measure = self.compile_capacity
        if role == "document_index" and self.index_capacity_policy is not None:
            measure = TokenMeasure(self.index_capacity_policy)
        if role in {"compile", "document_index"} and measure is not None:
            from ultrakb.execution_capacity import require_request_capacity

            require_request_capacity(measure, kwargs, operation=role)
        return kwargs

    @contextmanager
    def attempt(self, role: str, request: ModelRequest, logical_id: str):
        scope = active_scope() or self.scope
        if scope and self.scope and scope.kb_dir != self.scope.kb_dir:
            raise ValueError("Model execution belongs to another knowledge base")
        admission = self.budget.acquire(role)
        try:
            with self._observe_attempt(role, request, logical_id, scope, admission) as observed:
                yield observed
        finally:
            self.budget.abandon(admission)

    @contextmanager
    def _observe_attempt(self, role, request, logical_id, scope, admission):
        from litellm._urltrakb_auth import authentication_scope

        with (
            authentication_scope() as authentication,
            observe_model_call(
                self.bindings.model(role),
                request.operation + "." + request.stage,
                scope=scope,
                logical_call_id=logical_id,
                parent_call_id=request.parent_call_id,
                operation=request.operation,
                prompt_version=request.prompt_version,
                fingerprint=self.fingerprint,
                before_send=lambda count: (
                    self.reserve(role) if count else self.budget.sent(admission)
                ),
                validate_credentials=lambda headers: self.bindings.validate_request_credentials(
                    role, headers, selected_auth=authentication.kind
                ),
            ) as observed,
        ):
            yield observed

    def _result(self, response, observed, logical_id: str, on_response=None) -> CompletionResult:
        if observed is not None:
            observed.finish(response)
        if on_response is not None:
            on_response(response)
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

    def complete(self, role: str, request: ModelRequest, *, on_response=None) -> CompletionResult:
        self.policy.check()
        self.check_capacity_input(request.payload(), role=role)
        while not self._slots.acquire(timeout=0.05):
            self.policy.check()
        try:
            logical_id = uuid.uuid4().hex
            for retry in range(self.policy.retries + 1):
                self.policy.check()
                try:
                    with self.attempt(role, request, logical_id) as observed:
                        response = litellm.completion(**self._kwargs(role, request))
                        return self._result(response, observed, logical_id, on_response)
                except Exception as exc:
                    self.model_errors.append(exc)
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

    async def wait_response(self, awaitable, *, on_response=None):
        async def receive():
            result = await awaitable
            if on_response is not None:
                on_response(result)
            return result

        task = asyncio.create_task(receive())
        try:
            while not task.done():
                self.policy.check()
                await asyncio.wait({task}, timeout=0.05)
            return await task
        finally:
            if not task.done():
                from ultrakb.llm_usage_transport import settle_received

                if on_response is None:
                    task.cancel()
                await settle_received(task)

    async def acomplete(
        self, role: str, request: ModelRequest, *, on_response=None
    ) -> CompletionResult:
        self.policy.check()
        self.check_capacity_input(request.payload(), role=role)
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
                        return self._result(response, observed, logical_id, on_response)
                except Exception as exc:
                    self.model_errors.append(exc)
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


_ACTIVE: ContextVar[CompletionExecutor | None] = ContextVar("ultrakb_model_executor", default=None)


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
            ContextCapacityError,
        ),
    ):
        raise error


def executor_from_snapshot(snapshot, cancelled: Callable[[], bool]) -> CompletionExecutor:
    values = snapshot.values()
    config = values["effective"]
    from ultrakb.config_validation import validate_model_policy

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
        generation=config.get("role_generation"),
    )
    from ultrakb.execution_capacity import captured_capacity_policy, compile_measure

    executor = CompletionExecutor(bindings, policy)
    executor.compile_capacity = compile_measure(snapshot, bindings.model("compile"))
    executor.index_capacity_policy = captured_capacity_policy(
        snapshot, bindings.model("document_index")
    )
    return executor


def compiler_executor(model: str, bundle, options: dict) -> CompletionExecutor:
    """Direct legacy calls capture once; application calls use their task binding."""
    if executor := active_executor():
        return executor
    from ultrakb.llm_runtime import get_extra_headers, get_timeout

    bundle = bundle or LlmCredentialBundle(extra_headers=get_extra_headers(), timeout=get_timeout())
    values = asdict(bundle)
    for name in ("api_key", "base_url", "timeout", "extra_headers"):
        if name in options:
            values[name] = options.pop(name)
    return CompletionExecutor(RoleBindings(model, LlmCredentialBundle(**values)))
