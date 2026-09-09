"""Cancellable model waits inside disposable document workers.

Only the SDK call runs in a background thread: it has no document-processing
or wiki-write callback. On cancellation the owner unwinds and rolls back its
transaction; process exit then discards the outstanding network-only thread.
Never use this boundary to abandon a document operation or a writer thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import threading
from typing import Any

from openkb.cancellation import OperationCancelled, cancellation_scope

_CURRENT: contextvars.ContextVar[DocumentCancellation | None] = contextvars.ContextVar(
    "document_cancellation", default=None
)


class DocumentCancellation:
    def __init__(
        self,
        stopped: threading.Event,
        *,
        enabled: bool = True,
        budget_expired: threading.Event | None = None,
    ) -> None:
        self.stopped, self.enabled = stopped, enabled
        self.budget_expired = budget_expired
        self._stack = contextlib.ExitStack()
        self._installed = False

    def __enter__(self) -> DocumentCancellation:
        token = _CURRENT.set(self if self.enabled else None)
        self._stack.callback(_CURRENT.reset, token)
        if self.enabled:
            self._stack.enter_context(cancellation_scope(self._stopped))
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stack.close()

    def check(self) -> None:
        if self.enabled and self._stopped():
            raise OperationCancelled("Document processing stopped")

    def _stopped(self) -> bool:
        if self.budget_expired is not None and self.budget_expired.is_set():
            from openkb.processing import ProcessingIncomplete

            raise ProcessingIncomplete("time_budget_exhausted")
        return self.stopped.is_set()

    def install(self, sdk: Any) -> None:
        if self._installed:
            return
        self._installed = True
        original_sync, original_async = sdk.completion, sdk.acompletion

        @functools.wraps(original_sync)
        def synchronous(*args: Any, **kwargs: Any) -> Any:
            self.check()
            done = threading.Event()
            value: list[Any] = []
            errors: list[BaseException] = []
            context = contextvars.copy_context()

            def request():
                try:
                    value.append(context.run(original_sync, *args, **kwargs))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    done.set()

            threading.Thread(target=request, name="openkb-model-request", daemon=True).start()
            while not done.wait(0.05):
                self.check()
            self.check()
            if errors:
                raise errors[0]
            return value[0]

        @functools.wraps(original_async)
        async def asynchronous(*args: Any, **kwargs: Any) -> Any:
            self.check()
            pending = asyncio.ensure_future(original_async(*args, **kwargs))
            try:
                while not pending.done():
                    self.check()
                    await asyncio.wait({pending}, timeout=0.05)
                self.check()
                return pending.result()
            finally:
                if not pending.done():
                    pending.cancel()
                # Native async transport cancellation closes its request before
                # returning ownership to the document transaction.
                await asyncio.gather(pending, return_exceptions=True)

        sdk.completion, sdk.acompletion = synchronous, asynchronous
        self._stack.callback(setattr, sdk, "completion", original_sync)
        self._stack.callback(setattr, sdk, "acompletion", original_async)


def install_model_cancellation(sdk: Any) -> None:
    current = _CURRENT.get()
    if current is not None:
        current.install(sdk)
