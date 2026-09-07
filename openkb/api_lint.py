"""Established REST lint payload projection over shared maintenance outcomes."""

from pathlib import Path

from openkb.application.maintenance import LintOptions, check_knowledge


def fix_summary(files_changed: int | None, ghosts: int | None) -> str:
    if files_changed:
        return f"Fixed {ghosts} wikilink(s) across {files_changed} file(s)."
    return "Nothing to fix — all wikilinks resolve."


def echo_lint_event(event: dict) -> None:
    import click

    stage = event.get("stage")
    if stage == "structural_lint":
        click.echo("Running structural lint...")
    elif stage == "semantic_lint":
        click.echo("Running knowledge lint...")
    elif stage in {"structural_report", "semantic_report"}:
        click.echo(event["report"])


async def run_lint_report(
    kb_dir: Path, *, fix: bool = False, echo: bool = False, bundle=None, prepare_model=None
) -> dict:
    result = await check_knowledge(
        kb_dir,
        LintOptions(fix=fix),
        bundle=bundle,
        on_event=echo_lint_event if echo else None,
        prepare_model=prepare_model,
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
        message = f"{fix_summary(result.files_changed, result.ghosts_removed)} {message}"
    if echo:
        import click

        click.echo(message if skipped else f"\nReport written to {result.report_path}")
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
