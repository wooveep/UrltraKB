"""Meter secondary protocols against both document and task-family allowances."""

from contextlib import contextmanager
from typing import Any

from openkb.processing import ProcessingIncomplete, processing_checkpoint


@contextmanager
def external_request_usage(reservation: int, stage: str = "external", *, model_time=True):
    """Reserve and settle a secondary protocol request inside the active task budget."""
    from openkb.processing import _ACTIVE

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
        from openkb.runtime.family_budget import current_family

        family = current_family()
        family_key = (
            family.reserve(
                active.limits,
                reservation,
                stage,
                active.limits.request_timeout,
                model_time=model_time,
            )
            if family
            else None
        )
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
                if family_key:
                    family.settle(family_key, tokens, stage)
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
