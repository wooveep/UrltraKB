"""Cooperative operation cancellation that bypasses ordinary retry handlers."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_CANCELLED: ContextVar[Callable[[], bool] | None] = ContextVar("operation_cancelled", default=None)


class OperationCancelled(BaseException):
    """Unwind a transaction without treating a stop request as a model failure."""


@contextmanager
def cancellation_scope(cancelled: Callable[[], bool]) -> Iterator[None]:
    previous = _CANCELLED.get()
    token = _CANCELLED.set(lambda: bool(previous and previous()) or cancelled())
    try:
        yield
    finally:
        _CANCELLED.reset(token)


def check_cancelled() -> None:
    cancelled = _CANCELLED.get()
    if cancelled is not None and cancelled():
        raise OperationCancelled("Document processing stopped")
