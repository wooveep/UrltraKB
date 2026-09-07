"""Knowledge checks with separately committed link repair and report outcomes."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal

from openkb.application.execution import ExecutionContext
from openkb.application.file_state import changed_files, contained_paths, file_versions
from openkb.config import LlmCredentialBundle, resolve_effective_config
from openkb.lint import fix_broken_links, run_structural_lint
from openkb.locks import LockCancelled, async_kb_lock, atomic_write_text, kb_read_lock
from openkb.log import append_log
from openkb.mutation import RecoveryRequired, mutation_scope


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


def _wiki_version(kb_dir: Path) -> str:
    versions = file_versions(kb_dir, [kb_dir / "wiki"])
    return hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()


def preview_link_repair(kb_dir: Path) -> tuple[str, str]:
    """Bind overwrite consent to the existing wiki and show its structural issues."""
    root = kb_dir.resolve()
    with kb_read_lock(root / ".openkb"):
        return _wiki_version(root), run_structural_lint(root)


async def check_knowledge(
    kb_dir: Path,
    options: LintOptions = LintOptions(),
    *,
    context: ExecutionContext | None = None,
    bundle: LlmCredentialBundle | None = None,
    on_event: Callable[[dict], None] | None = None,
    prepare_model: Callable[[], None] | None = None,
) -> LintResult:
    root = kb_dir.resolve()
    wiki = root / "wiki"
    cancelled = context.cancelled if context else None
    on_wait = context.waiting if context else None
    emit = context.on_event if context else on_event or (lambda event: None)
    async with async_kb_lock(
        root / ".openkb", exclusive=True, cancelled=cancelled, on_wait=on_wait
    ):
        if options.version is not None and _wiki_version(root) != options.version:
            return LintResult("conflict")
        registry = root / ".openkb/hashes.json"
        hashes = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
        if not isinstance(hashes, dict) or any(
            not isinstance(meta, dict) for meta in hashes.values()
        ):
            raise ValueError("Invalid document registry")
        if not hashes and options.semantic and not options.fix:
            return LintResult("skipped")
        # Validate paths before snapshotting or executing a repair. Link repair
        # may change any Markdown page except the core's established exclusions.
        pages = contained_paths(root, list(wiki.rglob("*.md")))
        result = LintResult("completed")
        stage = "link_repair" if options.fix else "structural_lint"
        pending = ["link_repair"] if options.fix else []
        if prepare_model and options.semantic:
            pending.append("configuration")
        pending.append("structural_lint")
        if options.semantic:
            pending.append("semantic_lint")
        pending.append("report")
        with context.begin(root) if context else nullcontext(bundle) as active_bundle:
            try:
                if options.fix:
                    emit({"stage": stage})
                    before = file_versions(root, pages)
                    with mutation_scope(root, pages, operation="repair-links"):
                        files, ghosts = fix_broken_links(wiki)
                        changes = changed_files(root, pages, before)
                    result = replace(
                        result, files_changed=files, ghosts_removed=ghosts, changes=changes
                    )
                    pending.remove("link_repair")
                    emit({"stage": "links_repaired", "files": files, "ghosts": ghosts})
                if not hashes and options.semantic:
                    return replace(result, status="skipped")
                # Legacy CLI model globals are initialized after its independent
                # fix commit. Desktop configuration comes from context.begin.
                if prepare_model and options.semantic:
                    prepare_model()
                    pending.remove("configuration")
                stage = "structural_lint"
                emit({"stage": stage})
                result = replace(result, structural_report=run_structural_lint(root))
                pending.remove("structural_lint")
                emit({"stage": "structural_report", "report": result.structural_report})
                if options.semantic:
                    stage = "semantic_lint"
                    emit({"stage": stage})
                    from openkb.agent.linter import run_knowledge_lint
                    from openkb.agent.query import build_run_config_from_bundle

                    config = (await asyncio.to_thread(resolve_effective_config, root))[0]
                    model = config["model"]
                    issues: list[str] = []
                    try:
                        knowledge = await run_knowledge_lint(
                            root,
                            model,
                            bundle=active_bundle,
                            run_config=build_run_config_from_bundle(model, active_bundle),
                            on_issue=issues.append,
                        )
                    except Exception as exc:
                        knowledge = (
                            f"Knowledge lint failed ({type(exc).__name__})"
                            if context
                            else f"Knowledge lint failed: {exc}"
                        )
                        issues.append("semantic_lint_failed")
                    if issues:
                        result = replace(
                            result, quality=tuple(issues), unfinished=("semantic_lint",)
                        )
                    result = replace(result, knowledge_report=knowledge)
                    pending.remove("semantic_lint")
                    emit({"stage": "semantic_report", "report": knowledge})
                stage = "report"
                emit({"stage": "saving_report"})
                report_path = _save_report(root, result, unique=options.unique_report)
                return replace(
                    result,
                    report_path=str(report_path),
                    resources=(str(report_path),),
                    changes=(*result.changes, f"saved: {report_path.relative_to(root).as_posix()}"),
                )
            except LockCancelled:
                raise
            except Exception as exc:
                return replace(
                    result,
                    status="blocked" if isinstance(exc, RecoveryRequired) else "failed",
                    error_type=type(exc).__name__,
                    unfinished=tuple(dict.fromkeys((*result.unfinished, *pending))),
                )


def _save_report(root: Path, result: LintResult, *, unique: bool) -> Path:
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    directory = root / "wiki/reports"
    if directory.exists() and not directory.is_dir():
        raise NotADirectoryError("The report directory is occupied by a file")
    path = directory / f"lint_{timestamp}.md"
    counter = 1
    while unique and path.exists():
        path = directory / f"lint_{timestamp}_{counter}.md"
        counter += 1
    log = root / "wiki/log.md"
    contained_paths(root, [path, log])
    content = f"# Lint Report — {timestamp}\n\n## Structural\n\n{result.structural_report}\n"
    if result.knowledge_report is not None:
        content += f"\n## Semantic\n\n{result.knowledge_report}\n"
    with mutation_scope(root, [path, log], operation="lint-report"):
        atomic_write_text(path, content)
        append_log(root / "wiki", "lint", f"report → {path.name}")
    return path
