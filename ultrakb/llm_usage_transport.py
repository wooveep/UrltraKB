"""Observe HTTPX sends inside a single import call, including SDK retries.

The process hooks are inert outside the ContextVar scope. Each actual send gets
its own request ID before the network operation. Providers using another send
library retain call-level records marked collection_complete=False.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ultrakb.execution_timing import ObservationStart, record_phase_event, start_observation
from ultrakb.llm_usage import (
    _SCOPE,
    UsageScope,
    active_scope,
    begin_request,
    finish_request,
    usage_scope_lifecycle,
)


@dataclass
class ModelCall:
    scope: UsageScope | None
    model: str
    stage: str
    first_id: str
    sent_ids: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    before_send: Callable[[int], None] | None = None
    validate_credentials: Callable[[Mapping[str, str]], None] | None = None
    send_error: BaseException | None = None
    raw_usage_available: bool | None = None
    image_digests: set[str] | None = None
    response_status: int | None = None
    native_response_status: str | None = None
    cleanup_deadline: float | None = None
    send_starts: dict[str, ObservationStart] = field(default_factory=dict)
    response_ids: set[str] = field(default_factory=set)
    retained_responses: set[str] = field(default_factory=set)
    body_receipts: list[Any] = field(default_factory=list)

    def observe_images(self, request):
        import json

        from ultrakb.llm_images import request_image_digests

        body = json.loads(request.content)
        self.image_digests = request_image_digests(
            [body[key] for key in ("messages", "input", "contents") if key in body]
        )

    def check_credentials(self, headers):
        if self.validate_credentials is not None:
            try:
                self.validate_credentials(headers)
            except BaseException as exc:
                self.send_error = exc
                raise

    def sending(self):
        with usage_scope_lifecycle(self.scope):
            return self._sending()

    def _sending(self):
        if self.before_send is not None:
            try:
                self.before_send(len(self.sent_ids))
            except BaseException as exc:
                self.send_error = exc
                raise
        identity = (
            self.first_id
            if not self.sent_ids
            else begin_request(self.model, self.stage, scope=self.scope, **self.metadata)
            or uuid.uuid4().hex
        )
        if self.scope is not None and self.scope.storage is not None:
            try:
                self.scope.storage.sending(identity)
            except BaseException as error:
                self.send_error = error
                raise
            self.sent_ids.append(identity)
        else:
            self.sent_ids.append(identity)
            finish_request(identity, scope=self.scope, terminal=False, send_status="sent")
        self.send_starts[identity] = start_observation()
        record_phase_event(
            "send",
            stage=self.stage,
            work_id=self.stage.split(".", 1)[-1],
            scope=self.scope,
            logical_call_id=self.metadata.get("logical_call_id"),
            request_attempt_id=identity,
            at=self.send_starts[identity],
        )
        return identity

    def finish(self, response=None, *, state="completed"):
        for body in self.body_receipts:
            body.finish(complete=False)
        identity = self.sent_ids[-1] if self.sent_ids else self.first_id
        if (
            response is not None
            and self.scope is not None
            and self.scope.storage is not None
            and identity not in self.retained_responses
        ):
            import json

            if hasattr(response, "model_dump_json"):
                encoded = response.model_dump_json().encode()
            else:
                encoded = json.dumps(response).encode()
            hidden = getattr(response, "_hidden_params", {})
            cache_hit = (
                not self.sent_ids and isinstance(hidden, dict) and hidden.get("cache_hit") is True
            )
            self.capture_response(
                identity,
                encoded,
                observed_response=response if not self.sent_ids else None,
                state=state,
                send_status="sent" if self.sent_ids else "not_sent" if cache_hit else "unknown",
                cache_hit=cache_hit,
            )
        if self.sent_ids:
            self.response_event(
                self.sent_ids[-1],
                state,
                response_observed=response is not None or self.response_status is not None,
            )
        # A consumed HTTP response is authoritative. SDKs can synthesize zero
        # usage when the provider omitted it; that must remain unknown here.
        finish_request(
            self.sent_ids[-1] if self.sent_ids else self.first_id,
            None if self.sent_ids or identity in self.retained_responses else response,
            state=state,
            scope=self.scope,
            business_state=state,
        )

    def received(self, identity, response, *, complete=True):
        try:
            return self._received(identity, response, complete=complete)
        except BaseException as error:
            if self.scope is not None and self.scope.storage is not None:
                self.send_error = self.scope.storage.failure or error
            raise

    def _received(self, identity, response, *, complete=True):
        received = start_observation()
        state = "completed" if response.is_success and complete else "failed"
        self.response_status = response.status_code
        raw = _response(response)
        if isinstance(raw, dict) and raw.get("object") == "response":
            status = raw.get("status")
            self.native_response_status = status if isinstance(status, str) else None
        self.raw_usage_available = isinstance(raw, dict) and raw.get("usage") is not None
        if raw is None and response.is_stream_consumed:
            import json

            from ultrakb.stream_receipts import merge_receipts

            events = []
            for line in response.text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    events.append(json.loads(line[5:]))
                except ValueError:
                    continue
            merged = merge_receipts(events)
            if merged:
                raw = merged
                self.raw_usage_available = True
        if response.is_stream_consumed:
            self.capture_response(
                identity,
                response.content,
                observed_response=raw,
                state=state,
                observation="transport",
            )
        finish_request(
            identity,
            raw,
            state=state,
            scope=self.scope,
            observation="transport",
            terminal=response.is_stream_consumed,
        )
        if response.is_stream_consumed:
            self.response_event(
                identity,
                state,
                at=received,
                response_observed=True,
            )

    def capture_response(self, identity, content, **receipt):
        if self.scope is None or self.scope.storage is None:
            return
        try:
            self.scope.storage.capture(identity, content, **receipt)
        except BaseException as error:
            self.send_error = error  # SDK wrappers must not turn persistence failure into a retry.
            raise
        self.retained_responses.add(identity)

    def response_event(self, identity, state, *, at=None, response_observed=False):
        if identity in self.response_ids or identity not in self.send_starts:
            return
        self.response_ids.add(identity)
        record_phase_event(
            "response" if response_observed else "request_settled",
            stage=self.stage,
            work_id=self.stage.split(".", 1)[-1],
            scope=self.scope,
            logical_call_id=self.metadata.get("logical_call_id"),
            request_attempt_id=identity,
            started=self.send_starts[identity],
            outcome=state,
            at=at,
        )


_CALL: ContextVar[ModelCall | None] = ContextVar("ultrakb_usage_model_call", default=None)
_GUARD = threading.Lock()
_INSTALLED = False


async def settle_received(task):
    """Drain only an already-sent response, sharing a 250 ms cleanup deadline."""
    import asyncio

    call = _CALL.get()
    loop = asyncio.get_running_loop()
    if call and call.sent_ids:
        if call.cleanup_deadline is None:
            call.cleanup_deadline = loop.time() + 0.25
        while not task.done() and (remaining := call.cleanup_deadline - loop.time()) > 0:
            try:
                await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                continue  # Repeated stop requests cannot abandon cleanup or the KB lease.
    if not task.done():
        task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if task.done() and not task.cancelled():
        task.exception()  # Consume cleanup errors; preserve the original terminal cause.


def _response(response):
    try:
        return response.json() if response.is_stream_consumed else None
    except (ValueError, UnicodeError):
        return None


def _model_request(request):
    import json

    try:
        value = json.loads(request.content)
        return isinstance(value, dict) and any(
            key in value for key in ("messages", "input", "prompt", "contents")
        )
    except (ValueError, UnicodeError):
        return False


def _failure_state(exc):
    import asyncio

    from ultrakb.locks import LockCancelled

    return (
        "cancelled"
        if isinstance(exc, (asyncio.CancelledError, LockCancelled, KeyboardInterrupt))
        else "failed"
    )


def install_send_observer():
    global _INSTALLED
    with _GUARD:
        if _INSTALLED:
            return
        import httpx

        sync_send, async_send = httpx.Client.send, httpx.AsyncClient.send

        def send(client, request, *args, **kwargs):
            call = _CALL.get()
            if call is not None:
                call.check_credentials(request.headers)
            if call is None or request.method != "POST" or not _model_request(request):
                return sync_send(client, request, *args, **kwargs)
            identity = call.sending()
            call.observe_images(request)
            # Redirect replays occur below HTTPX.send and would bypass our
            # per-send budget. A model endpoint must be configured directly.
            kwargs["follow_redirects"] = False
            try:
                response = sync_send(client, request, *args, **kwargs)
            except BaseException as exc:
                call.response_event(identity, _failure_state(exc))
                finish_request(
                    identity, state=_failure_state(exc), scope=call.scope, observation="transport"
                )
                raise
            call.received(identity, response)
            from ultrakb.llm_usage_stream import observe_response_stream

            observe_response_stream(call, identity, response)
            return response

        async def asend(client, request, *args, **kwargs):
            import asyncio

            call = _CALL.get()
            if call is not None:
                call.check_credentials(request.headers)
            if call is None or request.method != "POST" or not _model_request(request):
                return await async_send(client, request, *args, **kwargs)
            identity = call.sending()
            call.observe_images(request)
            kwargs["follow_redirects"] = False

            async def receive():
                response = await async_send(client, request, *args, **kwargs)
                call.received(identity, response)
                from ultrakb.llm_usage_stream import observe_response_stream

                observe_response_stream(call, identity, response)
                return response

            pending = asyncio.create_task(receive())
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                await settle_received(pending)
                if pending.done() and not pending.cancelled() and pending.exception() is None:
                    response = pending.result()
                    if not response.is_stream_consumed:

                        async def receive_body():
                            await response.aread()
                            call.received(identity, response)

                        await settle_received(asyncio.create_task(receive_body()))
                    await response.aclose()
                finish_request(
                    identity,
                    state="cancelled",
                    scope=call.scope,
                    observation="transport",
                    business_state="cancelled",
                )
                raise
            except BaseException as exc:
                call.response_event(identity, _failure_state(exc))
                finish_request(
                    identity, state=_failure_state(exc), scope=call.scope, observation="transport"
                )
                raise

        httpx.Client.send, httpx.AsyncClient.send = send, asend
        _INSTALLED = True


@contextmanager
def observe_model_call(
    model: str,
    stage: str,
    *,
    scope=None,
    before_send=None,
    validate_credentials=None,
    logical_call_id=None,
    parent_call_id=None,
    operation=None,
    prompt_version=None,
    fingerprint=None,
):
    scope = scope or active_scope()
    if scope is None and before_send is None and validate_credentials is None:
        yield None
        return
    install_send_observer()
    metadata = dict(
        logical_call_id=logical_call_id,
        parent_call_id=parent_call_id,
        operation=operation,
        prompt_version=prompt_version,
        policy_fingerprint=fingerprint,
    )
    identity = (
        begin_request(model, stage, scope=scope, send_status="not_sent", **metadata)
        or uuid.uuid4().hex
    )
    call = ModelCall(
        scope,
        model,
        stage,
        identity,
        metadata=metadata,
        before_send=before_send,
        validate_credentials=validate_credentials,
    )
    token = _CALL.set(call)
    try:
        with usage_scope_lifecycle(scope):
            yield call
    except BaseException as exc:
        call.finish(state=_failure_state(exc))
        if call.send_error is not None:
            raise call.send_error from exc
        raise
    finally:
        _CALL.reset(token)


class IndexUsageObserver:
    def __init__(self):
        self.scope = active_scope()

    @contextmanager
    def call(self, model, stage):
        token = _SCOPE.set(self.scope)
        try:
            with observe_model_call(model, stage) as call:
                yield call
        finally:
            _SCOPE.reset(token)
