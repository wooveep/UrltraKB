"""A smaller operation allowance enforced inside every transport reservation and wait."""

from contextlib import contextmanager
from contextvars import ContextVar

_ALLOWANCE = ContextVar("openkb_request_allowance", default=None)


def active_allowance():
    return _ALLOWANCE.get()


@contextmanager
def request_allowance(value):
    token = _ALLOWANCE.set(value)
    try:
        yield
    finally:
        _ALLOWANCE.reset(token)
