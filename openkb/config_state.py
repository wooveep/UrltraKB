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
from dataclasses import asdict, dataclass, field
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
        from openkb.config import (
            resolve_per_request_overrides,
            set_extra_headers,
            set_parallel_tool_calls,
            set_timeout,
        )

        values = self.values()
        os.environ.clear()
        os.environ.update(values["environment"])
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


_ACTIVE: ContextVar[ConfigSnapshot | None] = ContextVar("openkb_config_snapshot", default=None)


def active_values(kb_dir: Path) -> dict[str, Any] | None:
    snapshot = _ACTIVE.get()
    if snapshot is None:
        return None
    if str(kb_dir.resolve()) != snapshot.kb_dir:
        raise ValueError("An execution cannot access another knowledge base's configuration")
    return snapshot.values()


def capture_config(kb_dir: Path) -> ConfigSnapshot:
    """Capture settings after KB recovery while holding execution permission.

    Global saves use their existing lock. Before/after byte comparisons also
    reject a racing external edit instead of mixing files from two versions.
    """
    from dotenv import dotenv_values

    from openkb import config
    from openkb.locks import kb_ingest_lock_held

    kb_dir = kb_dir.resolve()
    if not kb_ingest_lock_held(kb_dir / ".openkb"):
        raise RuntimeError("Configuration is fixed only after acquiring execution permission")
    paths = (
        kb_dir / ".openkb/config.yaml",
        kb_dir / ".env",
        config.GLOBAL_CONFIG_PATH,
        config.GLOBAL_CONFIG_DIR / ".env",
    )

    def read_versions() -> list[bytes | None]:
        return [path.read_bytes() if path.exists() else None for path in paths]

    with config._with_global_config_lock():
        for _ in range(3):
            before = read_versions()
            effective, sources = config.resolve_effective_config(kb_dir)
            raw = config.load_config(paths[0])
            environment = dict(os.environ)
            for key, value in dotenv_values(paths[3]).items():
                if value and not environment.get(key):
                    environment[key] = value
            for key, value in dotenv_values(paths[1]).items():
                if value:
                    environment[key] = value
            bundle = config.resolve_credential_bundle(kb_dir)
            if read_versions() != before:
                continue
            if bundle.api_key:
                provider = str(effective["model"]).split("/")[0].upper()
                if "/" not in str(effective["model"]):
                    provider = "OPENAI"
                environment[f"{provider}_API_KEY"] = bundle.api_key
                environment.setdefault("OPENAI_API_KEY", bundle.api_key)
            return ConfigSnapshot(
                kb_dir=str(kb_dir),
                _payload=json.dumps(
                    {
                        "effective": effective,
                        "sources": sources,
                        "raw": raw,
                        "credentials": asdict(bundle),
                        "environment": environment,
                    },
                    ensure_ascii=False,
                ),
            )
    raise ValueError("Settings changed during capture; try starting the task again")
