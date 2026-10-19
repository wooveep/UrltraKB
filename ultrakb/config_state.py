"""Immutable, in-memory settings for a single application execution.

Core configuration readers consult this context before reading files. Context
propagates to asyncio tasks; it contains no ownership or write-lock privilege.
Only an isolated worker may install its captured process environment.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass(frozen=True)
class ConfigSnapshot:
    kb_dir: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _payload: str = field(default="{}", repr=False)

    def values(self) -> dict[str, Any]:
        """Return an independent copy; nested settings cannot mutate the snapshot."""
        return json.loads(self._payload)

    @contextmanager
    def activate(self) -> Iterator[None]:
        token = _ACTIVE.set(self)
        try:
            yield
        finally:
            _ACTIVE.reset(token)

    def install_worker_environment(self) -> None:
        """Apply process globals only inside a fresh spawn worker."""
        import multiprocessing

        if multiprocessing.parent_process() is None:
            raise RuntimeError("Process settings require an isolated worker")
        from ultrakb.config import (
            resolve_per_request_overrides,
            set_extra_headers,
            set_parallel_tool_calls,
            set_timeout,
        )

        values = self.values()
        os.environ.clear()
        os.environ.update(values["environment"])
        from ultrakb.runtime.assets import configure_sdk_resources

        configure_sdk_resources()
        # Import after installing environment: SDK modules may read settings
        # at import time. This process is never reused for another unit.
        import litellm
        from agents import set_tracing_disabled

        set_tracing_disabled(True)
        bundle = values["credentials"]
        litellm.api_key = bundle["api_key"]
        set_extra_headers(bundle["extra_headers"])
        set_timeout(bundle["timeout"])
        set_parallel_tool_calls(
            bundle["parallel_tool_calls"], bundle["parallel_tool_calls_explicit"]
        )
        _, _, settings = resolve_per_request_overrides(values["effective"])
        for key, value in settings.items():
            if hasattr(litellm, key) and not callable(getattr(litellm, key)):
                setattr(litellm, key, value)


_ACTIVE: ContextVar[ConfigSnapshot | None] = ContextVar("ultrakb_config_snapshot", default=None)


def active_values(kb_dir: Path) -> dict[str, Any] | None:
    snapshot = _ACTIVE.get()
    if snapshot is None:
        return None
    if str(kb_dir.resolve()) != snapshot.kb_dir:
        raise ValueError("An execution cannot access another knowledge base's configuration")
    return snapshot.values()


def execution_environment(kb_dir: Path) -> dict[str, str]:
    """Read captured SDK settings without mutating the caller's process."""
    captured = active_values(kb_dir)
    return captured["environment"] if captured else dict(os.environ)


@contextmanager
def live_configuration() -> Iterator[None]:
    """Settings management and fresh capture read live files outside a parent recipe."""
    token = _ACTIVE.set(None)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def capture_config(kb_dir: Path, **wait_options) -> ConfigSnapshot:
    """Capture one prepared native database's immutable execution settings."""
    from ultrakb.application.execution_configuration import capture_storage_config
    from ultrakb.storage.database import open_database

    with open_database(kb_dir, read_only=True) as db:
        identity = db.identity
    return capture_storage_config(kb_dir, identity, **wait_options)
