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

from ultrakb.llm_execution import CompletionExecutor, ModelRequest
from ultrakb.llm_usage import finish_request, normalize_usage
from ultrakb.model_timing import ModelSlot


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
        self.response_accepted = False

    def receipt(self, response):
        if getattr(response, "id", None) and isinstance(getattr(response, "choices", None), list):
            self.response_accepted = True
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
                chunk = await self.executor.wait_response(
                    anext(self.raw_stream), on_response=self.receipt
                )
            except StopAsyncIteration:
                break
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
            if self.raw_usage is None and self.observed and self.observed.sent_ids:
                import asyncio

                from ultrakb.llm_usage_transport import settle_received

                async def remaining_receipts():
                    async for chunk in self.raw_stream:
                        self.receipt(chunk)
                        if self.raw_usage is not None:
                            break

                await settle_received(asyncio.create_task(remaining_receipts()))
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
        from ultrakb.agent.answer_finalization import AnswerFinisher

        self.finisher: AnswerFinisher | None = None
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
            from ultrakb.llm_execution import AnswerReserveReached

            if (
                isinstance(context.error, AnswerReserveReached)
                and self.finisher is not None
                and not self.finisher.returned
            ):
                self.finisher.finish("call_budget")
                return True
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

        def argument(name, index):
            return kwargs[name] if name in kwargs else args[index] if len(args) > index else None

        def set_argument(name, index, value):
            if name in kwargs:
                kwargs[name] = value
            else:
                args[index] = value

        from ultrakb.agent.answer_finalization import FINAL_INSTRUCTION
        from ultrakb.model_tokenizer import ContextCapacityError

        if self.finisher:
            self.finisher.prepare(retry=self._logical.get() is not None)

        def capacity_payload():
            return {
                "instructions": argument("system_instructions", 0),
                "input": argument("input", 1),
                "tools": [
                    {
                        "name": getattr(t, "name", ""),
                        "description": getattr(t, "description", ""),
                        "parameters": getattr(t, "params_json_schema", {}),
                    }
                    for t in (argument("tools", 3) or [])
                ],
                "final_instruction_reserve": (
                    FINAL_INSTRUCTION + "\nExecution limit: context_capacity."
                    if self.finisher and not self.finisher.reason
                    else ""
                ),
                "generation": self.executor.bindings.generation_options("answer"),
            }

        if self.finisher and not self.finisher.reason:
            try:
                self.executor.check_capacity_input(capacity_payload())
            except ContextCapacityError:
                self.finisher.finish("context_capacity")
        if self.finisher and self.finisher.reason:
            set_argument(
                "system_instructions",
                0,
                (argument("system_instructions", 0) or "")
                + "\n"
                + FINAL_INSTRUCTION
                + f"\nExecution limit: {self.finisher.reason}.",
            )
            set_argument("tools", 3, [])
        self.executor.check_capacity_input(capacity_payload())
        settings = kwargs.get("model_settings") if "model_settings" in kwargs else args[2]
        if self.finisher and self.finisher.reason:
            settings = replace(settings, tool_choice="none", parallel_tool_calls=False)
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
        captured = self.executor.bindings.generation_options("answer")
        fields = {
            key: captured[key] for key in ("temperature", "top_p", "max_tokens") if key in captured
        }
        for key in fields:
            extras.pop(key, None)
        if "reasoning_effort" in captured:
            from openai.types.shared import Reasoning

            fields["reasoning"] = Reasoning(effort=captured["reasoning_effort"])
            extras.pop("reasoning_effort", None)
        if captured:
            extras["drop_params"] = False
        settings = replace(settings, **fields)
        request = ModelRequest(
            [{"role": "user", "content": ""}],
            operation="answer",
            stage="agent",
            prompt_version="agent-scope-v3",
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
                **(
                    {"allowed_openai_params": transport["allowed_openai_params"]}
                    if "allowed_openai_params" in transport
                    else {}
                ),
                **({"extra_body": transport["extra_body"]} if "extra_body" in transport else {}),
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
        from ultrakb.application.execution import activity

        with activity("answering"):
            async with ModelSlot(self.executor, request, logical):
                with self.executor.attempt("answer", request, logical) as observed:
                    inner = _ReceiptModel(self.executor, observed)
                    try:
                        response = await self.executor.wait_response(
                            inner.get_response(*args, **kwargs)
                        )
                        response.ultrakb_usage = inner.public_usage()
                        response.ultrakb_image_digests = observed.image_digests
                        if self.finisher:
                            self.finisher.receive(response.output)
                        observed.finish()
                        self.executor.policy.check()
                        self._logical.set(None)
                        return response
                    finally:
                        try:
                            await inner.close()
                        finally:
                            self.executor.image_deliveries.settle(
                                self.executor.image_deliveries.capture(
                                    kwargs.get("input", args[1] if len(args) > 1 else None)
                                ),
                                observed,
                                accepted=inner.response_accepted,
                            )

    async def stream_response(self, *args, **kwargs):
        args, kwargs, request = self._arguments(args, kwargs)
        logical = self._logical.get() or uuid.uuid4().hex
        self._logical.set(logical)
        from ultrakb.application.execution import activity

        with activity("answering"):
            async with ModelSlot(self.executor, request, logical):
                with self.executor.attempt("answer", request, logical) as observed:
                    inner = _ReceiptModel(self.executor, observed)
                    stream = inner.stream_response(*args, **kwargs)
                    try:
                        async for event in stream:
                            self.executor.policy.check()
                            if event.type == "response.completed":
                                event.response.ultrakb_usage = inner.public_usage()
                                event.response.ultrakb_image_digests = observed.image_digests
                                if self.finisher:
                                    self.finisher.receive(event.response.output)
                            yield event
                        observed.finish()
                        self._logical.set(None)
                    finally:
                        try:
                            await stream.aclose()
                            await inner.close()
                        finally:
                            self.executor.image_deliveries.settle(
                                self.executor.image_deliveries.capture(
                                    kwargs.get("input", args[1] if len(args) > 1 else None)
                                ),
                                observed,
                                accepted=inner.response_accepted,
                            )
