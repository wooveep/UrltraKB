"""Optional per-attempt receipt of the provider's selected authentication mode."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal

AuthKind = Literal["api_key", "sdk"]


@dataclass
class AuthenticationReceipt:
    kind: AuthKind | None = None


_RECEIPT: ContextVar[AuthenticationReceipt | None] = ContextVar(
    "litellm_authentication_receipt", default=None
)


@contextmanager
def authentication_scope():
    # The mutable receipt is shared with SDK worker contexts; a scalar
    # ContextVar update in a worker would not reach the async sender.
    receipt = AuthenticationReceipt()
    token = _RECEIPT.set(receipt)
    try:
        yield receipt
    finally:
        _RECEIPT.reset(token)


def selected_authentication(kind: AuthKind) -> None:
    receipt = _RECEIPT.get()
    if receipt is not None:
        receipt.kind = kind
