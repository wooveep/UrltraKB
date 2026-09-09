"""Shared source intake, parsing and private knowledge publication use cases."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openkb.application.execution import ExecutionContext
from openkb.compilation_report import collect_compile_report
from openkb.config import resolve_effective_config
from openkb.inputs import SUPPORTED_EXTENSIONS, prepared_input, validate_source_root
from openkb.locks import kb_ingest_lock

logger = logging.getLogger(__name__)


def add_single_file(
    file_path: Path,
    kb_dir: Path,
    *,
    stage: bool = True,
    bundle=None,
    report=logger.info,
    on_event: Callable[[dict], None] | None = None,
    origin_url: str | None = None,
) -> str:
    """Compatibility result adapter for callers of the shared import use case."""
    result = import_document(
        kb_dir, file_path, bundle=bundle, on_event=on_event, origin_url=origin_url, report=report
    )
    report(
        f"[{result.status.upper()}] {file_path.name}: "
        f"{result.reason or result.knowledge_compilation}"
    )
    return result.status


@dataclass
class AddFileResult:
    """Structured add outcome for the REST API.

    Wraps the plain ``Literal`` returned by the locked ``add_single_file`` with
    the original filename and a human-readable message, which the API's
    ``/add`` endpoint surfaces per file in its JSON/SSE response.
    """

    original_name: str
    saved_path: str | None
    status: str
    message: str


def _add_for_api(
    file_path: Path, kb_dir: Path, *, bundle=None, source_root: Path | None = None
) -> AddFileResult:
    """Present the shared import result to older API integrations."""
    result = import_document(kb_dir, file_path, bundle=bundle, source_root=source_root)
    status_str = result.status
    if status_str == "skipped":
        message = f"Already in knowledge base: {file_path.name}"
    elif status_str in {"failed", "unfinished"}:
        message = f"Failed to add: {file_path.name} (see server logs)"
    else:
        message = f"Added: {file_path.name}"
    return AddFileResult(
        original_name=file_path.name,
        saved_path=result.resources[0] if result.resources else None,
        status=status_str,
        message=message,
    )


@dataclass(frozen=True)
class DocumentResult:
    source: str
    status: str
    resources: tuple[str, ...]
    quality: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    input_version: str | None = None
    source_intake: str = "not_saved"
    knowledge_compilation: str = "not_started"
    stage: str = "preparing"
    reason: str | None = None
    resume: str | None = None
    warnings: tuple[str, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    source_id: str | None = None
    parse_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not isinstance(self.stage, str):
            raise ValueError("Invalid document source or stage")
        if self.status not in {"added", "skipped", "failed", "unfinished", "stopped"}:
            raise ValueError("Invalid document status")
        if self.source_intake not in {"saved", "not_saved"}:
            raise ValueError("Invalid source intake status")
        if self.knowledge_compilation not in {
            "completed",
            "not_started",
            "failed",
            "unfinished",
            "stopped",
        }:
            raise ValueError("Invalid knowledge compilation status")
        for value in (self.reason, self.resume, self.input_version, self.source_id, self.parse_id):
            if value is not None and not isinstance(value, str):
                raise ValueError("Invalid document result detail")
        for values in (self.resources, self.quality, self.unfinished, self.warnings):
            if not isinstance(values, tuple) or not all(isinstance(item, str) for item in values):
                raise ValueError("Invalid document result list")
        from openkb.processing import validate_usage

        validate_usage(self.usage)

    @classmethod
    def from_summary(cls, value: dict[str, Any]) -> DocumentResult:
        if not isinstance(value, dict):
            raise ValueError("Invalid document result")
        value = dict(value)
        for key in ("resources", "quality", "unfinished", "warnings"):
            if not isinstance(value.get(key, []), list):
                raise ValueError("Invalid document result list")
            value[key] = tuple(value.get(key, ()))
        return cls(**value)


def import_document(
    kb_dir: Path,
    source: Path,
    *,
    bundle=None,
    on_event: Callable[[dict], None] | None = None,
    context: ExecutionContext | None = None,
    origin_url: str | None = None,
    source_root: Path | None = None,
    source_origin: str | None = None,
    report=logger.info,
) -> DocumentResult:
    """Process one complete item and report only resources actually retained."""
    from openkb.sources import SourceStore

    if source_origin is not None and origin_url is not None:
        raise ValueError("Choose one source origin")
    root = kb_dir.expanduser().resolve()
    requested_source = source.expanduser().absolute()
    validate_source_root(requested_source, source_root)
    source = requested_source.resolve()
    if not (root / ".openkb/config.yaml").is_file():
        raise ValueError(f"Not a knowledge base: {root}")
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError("Unsupported document type")
    context = context or ExecutionContext()
    context.on_event({"stage": "preparing", "source": str(source)})
    context.check_stop()
    with prepared_input(source) as ready:
        with kb_ingest_lock(
            root / ".openkb",
            cancelled=context.cancelled if context else None,
            on_wait=context.waiting if context else None,
        ):
            validate_source_root(requested_source, source_root)
            if not ready.is_current():
                ready = ready.refresh()
            validate_source_root(requested_source, source_root)
            with (
                context.begin(root) as credentials,
                collect_compile_report(),
            ):
                context.on_event({"stage": "source_intake", "source": str(source)})
                store = SourceStore(root)
                source_version = store.intake(ready, origin=source_origin or origin_url)
                from openkb.application.document_pipeline import compile_version

                return compile_version(
                    root,
                    source_version,
                    resolve_effective_config(root)[0],
                    bundle=bundle or credentials,
                    on_event=on_event or context.on_event,
                    input_is_current=ready.is_current,
                )
