"""Join cancelled HTTP artifact reads and cancel their actual storage waits."""

from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any, TypeVar

from ultrakb.application.views import published_scope, run_scoped_operation
from ultrakb.context.workers import run_blocking

T = TypeVar("T")


async def artifact_read(operation: Callable[..., T], root: Path, *args: Any, **kwargs: Any) -> T:
    stopped = Event()

    def read() -> T:
        scope = published_scope(root, cancelled=stopped.is_set)
        assert scope.knowledge is not None
        return run_scoped_operation(
            lambda: operation(root, *args, scope=scope, **kwargs),
            scope=scope,
            cancelled=stopped.is_set,
        )

    return await run_blocking(read, on_cancel=stopped.set)
