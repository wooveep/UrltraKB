"""Leave document budget for later work, including automatic physical retries."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from openkb.processing_limits import ProcessingIncomplete


@dataclass
class Reservation:
    requests: int
    tokens: int
    attempts: int
    dispatched: int = 0


_ACTIVE: ContextVar[Reservation | None] = ContextVar("request_reservation", default=None)


@contextmanager
def reserve_later_work(*, requests=0, tokens=0, attempts):
    token = _ACTIVE.set(Reservation(requests, tokens, attempts))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def check_later_work(budget, cost):
    hold = _ACTIVE.get()
    if hold is None:
        return
    if hold.dispatched >= hold.attempts:
        raise ProcessingIncomplete("planning_recovery_budget", budget.stage)
    if (
        budget.limits.max_requests is not None
        and budget.attempts + 1 + hold.requests > budget.limits.max_requests
    ):
        raise ProcessingIncomplete("pages_request_reserved", budget.stage)
    if (
        budget.limits.max_tokens is not None
        and budget.charged_tokens + cost + hold.tokens > budget.limits.max_tokens
    ):
        raise ProcessingIncomplete("pages_tokens_reserved", budget.stage)
    hold.dispatched += 1


def retry_attempt_available():
    """Stop transport retries at the task cap while preserving their real failure."""
    hold = _ACTIVE.get()
    return hold is None or hold.dispatched < hold.attempts
