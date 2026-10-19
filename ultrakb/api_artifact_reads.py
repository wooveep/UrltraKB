"""Join cancelled HTTP artifact reads and cancel their actual storage waits."""

from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any, TypeVar

from ultrakb.application.views import published_scope
from ultrakb.context.workers import run_blocking
from ultrakb.storage.coordination import storage_stage

T = TypeVar("T")


async def artifact_read(operation: Callable[..., T], root: Path, *args: Any, **kwargs: Any) -> T:
    stopped = Event()

    def read() -> T:
        scope = published_scope(root, cancelled=stopped.is_set)
        assert scope.knowledge is not None
        with storage_stage(
            root, scope.knowledge.identity, read_only=True, cancelled=stopped.is_set
        ):
            return operation(root, *args, scope=scope, **kwargs)

    return await run_blocking(read, on_cancel=stopped.set)
