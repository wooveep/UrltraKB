"""Agents SDK model adapter sharing a task's bindings, budget and cancellation.

The SDK owns the tool loop and retry safety. Each invocation here is exactly
one attempt; the completion executor's retry loop is deliberately not used.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import replace

from agents.extensions.models.litellm_model import LitellmModel
from agents.models.interface import Model
from agents.retry import ModelRetryBackoffSettings, ModelRetrySettings

from openkb.llm_execution import CompletionExecutor, ModelRequest
from openkb.llm_usage import finish_request, normalize_usage


class _ReceiptModel(LitellmModel):
    """Keep the raw usage and own the SDK stream until it has been closed."""

    def __init__(self, executor, observed):
        bundle = executor.bindings.credentials()
        super().__init__(
            model=executor.bindings.model("answer"),
            api_key=bundle.api_key,
            base_url=bundle.base_url,
        )
        self.observed = observed
        self.executor = executor
        self.raw_usage = None
        self.raw_stream = None

    def receipt(self, response):
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.raw_usage = usage
            if self.observed:
                finish_request(
                    self.observed.sent_ids[-1]
                    if self.observed.sent_ids
                    else self.observed.first_id,
                    response,
                    scope=self.observed.scope,
                    terminal=False,
                )

    async def _chunks(self):
        while True:
            try:
                chunk = await self.executor.wait_response(anext(self.raw_stream))
            except StopAsyncIteration:
                break
            self.receipt(chunk)
            yield chunk

    async def _fetch_response(self, *args, **kwargs):
        result = await self.executor.wait_response(super()._fetch_response(*args, **kwargs))
        if isinstance(result, tuple):
            response, self.raw_stream = result
            return response, self._chunks()
        self.receipt(result)
        # Agents Usage arithmetic requires integers. This compatibility copy is
        # internal only; the ledger and public events retain the raw receipt.
        if result.usage is not None:
            result = result.model_copy(deep=True)
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if getattr(result.usage, name) is None:
                    setattr(result.usage, name, 0)
        return result

    async def close(self):
        if self.raw_stream is not None:
            await self.raw_stream.aclose()
        await super().close()

    def public_usage(self):
        usage = normalize_usage(self.raw_usage)
        return {
            "input_tokens": usage["input_total"],
            "output_tokens": usage["output_total"],
            "input_tokens_details": {"cached_tokens": usage["cached_input"]},
            "output_tokens_details": {"reasoning_tokens": usage["reasoning_output"]},
        }


class ManagedAgentModel(Model):
    def __init__(self, executor: CompletionExecutor):
        self.executor = executor
        self.model = executor.bindings.model("answer")
        self._logical: ContextVar[str | None] = ContextVar("agent_logical_call", default=None)
        bundle = executor.bindings.credentials()
        self._adviser = LitellmModel(
            model=self.model, api_key=bundle.api_key, base_url=bundle.base_url
        )

    def get_retry_advice(self, request):
        return self._adviser.get_retry_advice(request)

    def retry_settings(self):
        policy = self.executor.policy

        def retry(context):
            policy.check()
            return policy.retryable(context.error)

        return ModelRetrySettings(
            max_retries=policy.retries,
            policy=retry,
            backoff=ModelRetryBackoffSettings(
                initial_delay=policy.backoff, max_delay=10, multiplier=2, jitter=False
            ),
        )

    def _arguments(self, args, kwargs):
        args = list(args)
        settings = kwargs.get("model_settings") if "model_settings" in kwargs else args[2]
        # Only validated generation extras can pass through this algorithm seam.
        extras = dict(settings.extra_args or {})
        for key in (
            "api_key",
            "base_url",
            "api_base",
            "num_retries",
            "max_retries",
            "timeout",
            "extra_headers",
            "client",
        ):
            extras.pop(key, None)
        request = ModelRequest(
            [{"role": "user", "content": ""}],
            operation="answer",
            stage="agent",
            prompt_version="agent-v1",
            generation_options=extras,
        )
        transport = self.executor._kwargs("answer", request)
        settings = replace(
            settings,
            extra_args={
                **extras,
                "num_retries": 0,
                "max_retries": 0,
                **({"timeout": transport["timeout"]} if "timeout" in transport else {}),
            },
            extra_headers=transport.get("extra_headers"),
            include_usage=True,
        )
        if "model_settings" in kwargs:
            kwargs = {**kwargs, "model_settings": settings}
        else:
            args[2] = settings
        return args, kwargs, request

    async def get_response(self, *args, **kwargs):
        args, kwargs, request = self._arguments(args, kwargs)
        logical = self._logical.get() or uuid.uuid4().hex
        self._logical.set(logical)
        async with self.executor.slot():
            with self.executor.attempt("answer", request, logical) as observed:
                inner = _ReceiptModel(self.executor, observed)
                try:
                    response = await self.executor.wait_response(
                        inner.get_response(*args, **kwargs)
                    )
                    response.openkb_usage = inner.public_usage()
                    response.openkb_image_digests = observed.image_digests
                    observed.finish()
                    self.executor.policy.check()
                    self._logical.set(None)
                    return response
                finally:
                    await inner.close()

    async def stream_response(self, *args, **kwargs):
        args, kwargs, request = self._arguments(args, kwargs)
        logical = self._logical.get() or uuid.uuid4().hex
        self._logical.set(logical)
        async with self.executor.slot():
            with self.executor.attempt("answer", request, logical) as observed:
                inner = _ReceiptModel(self.executor, observed)
                stream = inner.stream_response(*args, **kwargs)
                try:
                    async for event in stream:
                        self.executor.policy.check()
                        if event.type == "response.completed":
                            event.response.openkb_usage = inner.public_usage()
                            event.response.openkb_image_digests = observed.image_digests
                        yield event
                    observed.finish()
                    self._logical.set(None)
                finally:
                    await stream.aclose()
                    await inner.close()
