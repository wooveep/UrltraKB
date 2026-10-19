"""Knowledge-base creation, reopening and inventory shared by all entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ultrakb.config import load_config, register_kb, validate_runtime_config
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope

from .storage_creation import initialize_kb as initialize_kb


def display_document_type(raw_type: str) -> str:
    if raw_type in {"long_pdf", "pageindex_cloud"}:
        return "pageindex"
    if raw_type in {
        "pdf",
        "docx",
        "md",
        "markdown",
        "html",
        "htm",
        "txt",
        "csv",
        "pptx",
        "xlsx",
        "xls",
    }:
        return "short"
    return raw_type


def open_kb(kb_dir: Path) -> Path:
    root = kb_dir.expanduser().resolve()
    from ultrakb.kb_format import require_current_kb
    from ultrakb.lifecycle import read_lifecycle

    require_current_kb(root)
    with read_lifecycle(root):
        if not (root / ".ultrakb/config.yaml").is_file():
            raise ValueError(f"Knowledge base configuration is missing: {root}")
        validate_runtime_config(load_config(root / ".ultrakb/config.yaml"), allow_inherited=True)
        register_kb(root)
    return root


def get_kb_list(
    kb_dir: Path, *, scope: KnowledgeScope | None = None, context=None
) -> dict[str, Any]:
    """Return a structured inventory of the knowledge base (REST ``/list``)."""
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    selected = scope
    assert selected.knowledge is not None
    cancelled = context.cancelled if context else None
    from ultrakb.storage.coordination import storage_stage

    from .storage_inventory import knowledge_inventory

    with storage_stage(
        selected.kb_dir, selected.knowledge.identity, read_only=True, cancelled=cancelled
    ):
        return knowledge_inventory(selected)


def get_kb_status(
    kb_dir: Path, *, scope: KnowledgeScope | None = None, context=None
) -> dict[str, Any]:
    """Return structured status for the knowledge base (REST ``/status``)."""
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    selected = scope
    assert selected.knowledge is not None
    cancelled = context.cancelled if context else None
    from ultrakb.storage.coordination import storage_stage

    from .storage_status import knowledge_status

    with storage_stage(
        selected.kb_dir, selected.knowledge.identity, read_only=True, cancelled=cancelled
    ):
        return knowledge_status(selected)
