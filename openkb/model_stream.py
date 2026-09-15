"""Collect compiler text while tracking real response activity, never retaining reasoning text."""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar
from types import SimpleNamespace

from openkb.cancellation import check_cancelled

_CURRENT: ContextVar[StreamActivity | None] = ContextVar("compiler_stream_activity", default=None)


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def current_activity():
    return _CURRENT.get()


class StreamActivity:
    def __init__(self):
        self.started = self.last_content = time.monotonic()
        self.lock = threading.RLock()
        self.fragments = 0
        self.content_characters = 0
        self.reasoning_characters = 0
        self.first_content_seconds = None
        self.kind = "waiting"
        self.notify = lambda: None
        self.finished = lambda response, error: None
        self.last_notice = 0.0

    def observe(self, delta):
        content = field(delta, "content")
        reasoning = field(delta, "reasoning_content") or field(delta, "reasoning")
        if not isinstance(reasoning, str):
            reasoning = ""
        blocks = field(delta, "thinking_blocks") or []
        if not reasoning and isinstance(blocks, list):
            reasoning = "".join(
                text for block in blocks if isinstance(text := field(block, "thinking"), str)
            )
        content = content if isinstance(content, str) else ""
        # Empty events, role announcements and keepalives are not model progress.
        if not content.strip() and not reasoning.strip():
            return
        now = time.monotonic()
        with self.lock:
            changed = self.kind != ("content" if content.strip() else "reasoning")
            self.kind = "content" if content.strip() else "reasoning"
            self.last_content = now
            self.fragments += 1
            self.content_characters += len(content)
            self.reasoning_characters += len(reasoning)
            if self.first_content_seconds is None:
                self.first_content_seconds = now - self.started
            report = changed or now - self.last_notice >= 15
            if report:
                self.last_notice = now
        if report:
            self.notify()

    def remaining(self, timeout):
        with self.lock:
            return self.last_content + timeout - time.monotonic()

    def snapshot(self):
        with self.lock:
            return {
                "kind": self.kind,
                "fragments": self.fragments,
                "content_characters": self.content_characters,
                "reasoning_characters": self.reasoning_characters,
                "first_content_seconds": self.first_content_seconds,
                "idle_seconds": max(0.0, time.monotonic() - self.last_content),
            }


def collect(function, options, activity):
    """Keep stream consumption inside the cancellable, budget-owned transport thread."""
    token = _CURRENT.set(activity)
    stream = wire = None
    response = error = None
    try:
        stream = function(**options)
        from openkb.stream_wire import observe_wire

        wire = observe_wire(stream)
        # Some adapters return a complete response despite the streaming flag.
        # Accept that one response; never send a second request as a fallback.
        choices = field(stream, "choices")
        if choices and field(choices[0], "message") is not None:
            response = stream
            return response
        parts = []
        finish = None
        usage = None
        model = fingerprint = None
        for chunk in stream:
            check_cancelled()
            model = field(chunk, "model") or model
            fingerprint = field(chunk, "system_fingerprint") or fingerprint
            provided = field(chunk, "usage")
            if provided is not None:
                # Do not manufacture zero usage when the provider omits counters.
                if all(
                    type(field(provided, k)) is int for k in ("prompt_tokens", "completion_tokens")
                ):
                    usage = provided
            for choice in field(chunk, "choices", []) or []:
                if field(choice, "index", 0) != 0:
                    continue
                delta = field(choice, "delta")
                if field(delta, "tool_calls"):
                    raise ValueError("Compiler text stream returned unexpected tool calls")
                activity.observe(delta)
                text = field(delta, "content")
                if isinstance(text, str):
                    parts.append(text)
                finish = field(choice, "finish_reason") or finish
        if wire is not None:
            usage = wire.usage
        if finish is None or (wire is not None and not wire.finished):
            # A disconnected stream is not a settled response, even if its
            # partial content happens to parse as valid JSON.
            raise TimeoutError("Model stream ended without a terminal response")
        if isinstance(usage, dict):
            from litellm.types.utils import Usage

            usage = Usage(**usage)
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="".join(parts)),
                    finish_reason=finish,
                )
            ],
            usage=usage,
            model=model,
            system_fingerprint=fingerprint,
        )
        return response
    except BaseException as exc:
        error = exc
        raise
    finally:
        # SDK wrappers do not all expose close; generators and response streams
        # that do are closed by their owning thread.
        close = getattr(stream, "close", None)
        if callable(close):
            close()
        try:
            if wire is not None:
                wire.close()
            activity.finished(response, error)
        finally:
            _CURRENT.reset(token)


def validate_activity(value):
    import math

    counts = {"fragments", "content_characters", "reasoning_characters"}
    if (
        not isinstance(value, dict)
        or set(value) != counts | {"kind", "first_content_seconds", "idle_seconds"}
        or value["kind"] not in {"waiting", "reasoning", "content"}
        or any(type(value[key]) is not int or value[key] < 0 for key in counts)
    ):
        raise ValueError("Invalid response activity")
    for key in ("first_content_seconds", "idle_seconds"):
        number = value[key]
        if number is None and key == "first_content_seconds":
            continue
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
            raise ValueError("Invalid response activity duration")
