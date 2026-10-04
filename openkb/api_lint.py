"""Established REST lint payload projection over shared maintenance outcomes."""

from pathlib import Path

from openkb.application.maintenance import LintOptions, check_knowledge, describe_repairs
from openkb.knowledge_scope import KnowledgeScope


async def run_lint_report(
    kb_dir: Path,
    *,
    fix: bool = False,
    bundle=None,
    scope: KnowledgeScope | None = None,
) -> dict:
    result = await check_knowledge(
        kb_dir,
        LintOptions(fix=fix),
        bundle=bundle,
        scope=scope,
    )
    if result.status in {"failed", "blocked", "conflict"}:
        raise RuntimeError(f"Lint failed ({result.error_type or result.status})")
    skipped = result.status == "skipped"
    message = (
        "Nothing to lint - no documents indexed yet. Run `openkb add` first."
        if skipped
        else "Lint report written."
    )
    if fix:
        message = f"{describe_repairs(result.files_changed, result.ghosts_removed)} {message}"
    return {
        "skipped": skipped,
        "reason": "no_documents_indexed" if skipped else None,
        "message": message,
        "structural_report": result.structural_report,
        "knowledge_report": result.knowledge_report,
        "report_path": result.report_path,
        "lint_files_changed": result.files_changed,
        "lint_ghosts_removed": result.ghosts_removed,
    }
