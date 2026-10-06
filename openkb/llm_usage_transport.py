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
from typing import Callable

from openkb.llm_usage import _SCOPE, UsageScope, active_scope, begin_request, finish_request


@dataclass
class ModelCall:
    scope: UsageScope | None
    model: str
    stage: str
    first_id: str
    sent_ids: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    before_send: Callable[[int], None] | None = None
    send_error: BaseException | None = None
    raw_usage_available: bool | None = None

    def sending(self):
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
        self.sent_ids.append(identity)
        return identity

    def finish(self, response=None, *, state="completed"):
        # A consumed HTTP response is authoritative. SDKs can synthesize zero
        # usage when the provider omitted it; that must remain unknown here.
        finish_request(
            self.sent_ids[-1] if self.sent_ids else self.first_id,
            None if self.sent_ids else response,
            state=state,
            scope=self.scope,
        )

    def received(self, identity, response):
        raw = _response(response)
        self.raw_usage_available = isinstance(raw, dict) and raw.get("usage") is not None
        finish_request(
            identity,
            raw,
            state="completed" if response.is_success else "failed",
            scope=self.scope,
            observation="transport",
        )


_CALL: ContextVar[ModelCall | None] = ContextVar("openkb_usage_model_call", default=None)
_GUARD = threading.Lock()
_INSTALLED = False


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

    from openkb.locks import LockCancelled

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
            if call is None or request.method != "POST" or not _model_request(request):
                return sync_send(client, request, *args, **kwargs)
            identity = call.sending()
            # Redirect replays occur below HTTPX.send and would bypass our
            # per-send budget. A model endpoint must be configured directly.
            kwargs["follow_redirects"] = False
            try:
                response = sync_send(client, request, *args, **kwargs)
            except BaseException as exc:
                finish_request(
                    identity, state=_failure_state(exc), scope=call.scope, observation="transport"
                )
                raise
            call.received(identity, response)
            return response

        async def asend(client, request, *args, **kwargs):
            call = _CALL.get()
            if call is None or request.method != "POST" or not _model_request(request):
                return await async_send(client, request, *args, **kwargs)
            identity = call.sending()
            kwargs["follow_redirects"] = False
            try:
                response = await async_send(client, request, *args, **kwargs)
            except BaseException as exc:
                finish_request(
                    identity, state=_failure_state(exc), scope=call.scope, observation="transport"
                )
                raise
            call.received(identity, response)
            return response

        httpx.Client.send, httpx.AsyncClient.send = send, asend
        _INSTALLED = True


@contextmanager
def observe_model_call(
    model: str,
    stage: str,
    *,
    scope=None,
    before_send=None,
    logical_call_id=None,
    parent_call_id=None,
    operation=None,
    prompt_version=None,
    fingerprint=None,
):
    scope = scope or active_scope()
    if scope is None and before_send is None:
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
    identity = begin_request(model, stage, scope=scope, **metadata) or uuid.uuid4().hex
    call = ModelCall(scope, model, stage, identity, metadata=metadata, before_send=before_send)
    token = _CALL.set(call)
    try:
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
