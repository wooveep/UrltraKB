"""Knowledge checks with separately committed link repair and report outcomes."""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from ultrakb.application.execution import ExecutionContext
from ultrakb.application.file_state import contained_paths, file_versions
from ultrakb.config import LlmCredentialBundle
from ultrakb.knowledge_scope import KnowledgeScope, resolve_scope
from ultrakb.locks import atomic_write_text
from ultrakb.log import append_log
from ultrakb.mutation import mutation_scope


@dataclass(frozen=True)
class LintOptions:
    fix: bool = False
    semantic: bool = True
    version: str | None = None
    unique_report: bool = True


@dataclass(frozen=True)
class LintResult:
    status: Literal["completed", "skipped", "conflict", "failed", "blocked"]
    structural_report: str | None = None
    knowledge_report: str | None = None
    report_path: str | None = None
    files_changed: int | None = None
    ghosts_removed: int | None = None
    resources: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()
    quality: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    error_type: str | None = None
    publication_id: str | None = None
    projection_pending: bool = False


def describe_repairs(files_changed: int | None, ghosts: int | None) -> str:
    if files_changed:
        return f"Fixed {ghosts} wikilink(s) across {files_changed} file(s)."
    return "Nothing to fix — all wikilinks resolve."


def _wiki_version(scope: KnowledgeScope) -> str:
    versions = file_versions(scope.kb_dir, [scope.wiki_dir])
    return hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()


def preview_link_repair(kb_dir: Path, *, scope: KnowledgeScope | None = None) -> tuple[str, str]:
    """Bind overwrite consent to the existing wiki and show its structural issues."""
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    kb_dir.resolve()
    from .storage_lint import preview_stored_lint

    return preview_stored_lint(scope)


async def check_knowledge(
    kb_dir: Path,
    options: LintOptions = LintOptions(),
    *,
    context: ExecutionContext | None = None,
    bundle: LlmCredentialBundle | None = None,
    on_event: Callable[[dict], None] | None = None,
    scope: KnowledgeScope | None = None,
) -> LintResult:
    scope = resolve_scope(kb_dir, scope)
    if scope.knowledge is None:
        raise ValueError("This operation requires a main-store knowledge base")
    context = context or ExecutionContext(on_event=on_event or (lambda event: None))
    kb_dir.resolve()
    from .storage_lint import check_stored_knowledge

    return await check_stored_knowledge(scope, options, context, bundle=bundle)


def _save_report(scope: KnowledgeScope, result: LintResult, *, unique: bool) -> Path:
    root = scope.kb_dir
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    directory = scope.wiki_dir / "reports"
    if directory.exists() and not directory.is_dir():
        raise NotADirectoryError("The report directory is occupied by a file")
    path = directory / f"lint_{timestamp}.md"
    counter = 1
    while unique and path.exists():
        path = directory / f"lint_{timestamp}_{counter}.md"
        counter += 1
    log = scope.wiki_dir / "log.md"
    contained_paths(root, [path, log])
    content = f"# Lint Report — {timestamp}\n\n## Structural\n\n{result.structural_report}\n"
    if result.knowledge_report is not None:
        content += f"\n## Semantic\n\n{result.knowledge_report}\n"
    with mutation_scope(root, [path, log], operation="lint-report"):
        atomic_write_text(path, content)
        append_log(scope.wiki_dir, "lint", f"report → {path.name}", scope=scope)
    return path
