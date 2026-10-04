"""Terminal projections of shared maintenance and knowledge-base operations."""

from pathlib import Path

import click

from openkb.application.knowledge_bases import display_document_type as _display_type
from openkb.application.maintenance import describe_repairs as fix_summary
from openkb.knowledge_scope import KnowledgeScope, resolve_scope


def echo_lint_event(event: dict) -> None:
    import click

    stage = event.get("stage")
    if stage == "structural_lint":
        click.echo("Running structural lint...")
    elif stage == "semantic_lint":
        click.echo("Running knowledge lint...")
    elif stage in {"structural_report", "semantic_report"}:
        click.echo(event["report"])


async def run_lint(
    kb_dir: Path, *, fix: bool = False, scope: KnowledgeScope | None = None
) -> Path | None:
    """CLI/chat projection; shared maintenance owns complete checks and commits."""
    scope = resolve_scope(kb_dir, scope)
    from openkb.application.maintenance import LintOptions, check_knowledge

    def event(value):
        if value.get("stage") == "links_repaired":
            click.echo(fix_summary(value["files"], value["ghosts"]))
        else:
            echo_lint_event(value)

    result = await check_knowledge(
        kb_dir,
        LintOptions(fix=fix),
        on_event=event,
        scope=scope,
    )
    if result.status == "skipped":
        click.echo("Nothing to lint — no documents indexed yet. Run `openkb add` first.")
        return None
    if result.status != "completed" or result.report_path is None:
        raise RuntimeError(f"Lint failed ({result.error_type or result.status})")
    click.echo(f"\nReport written to {result.report_path}")
    return Path(result.report_path)


def print_list(kb_dir: Path, *, scope: KnowledgeScope | None = None) -> None:
    """Print all documents in the knowledge base. Usable from CLI and chat REPL."""
    from openkb.application.knowledge_bases import get_kb_list

    inventory = get_kb_list(kb_dir, scope=scope)
    documents = inventory["documents"]
    if not documents:
        click.echo("No documents indexed yet.")
        return

    # Display documents table with count in header
    doc_count = len(documents)
    click.echo(f"Documents ({doc_count}):")
    click.echo(f"  {'Name':<40} {'Type':<12} {'Pages':<8}")
    click.echo(f"  {'-' * 40} {'-' * 12} {'-' * 8}")
    for meta in documents:
        name = meta.get("name", "unknown")
        raw_type = meta.get("type", "unknown")
        display = _display_type(raw_type)
        pages = meta.get("pages", "")
        pages_str = str(pages) if pages else ""
        click.echo(f"  {name:<40} {display:<12} {pages_str:<8}")
        if meta.get("tokens") is not None:
            click.echo(
                f"    Text: {meta.get('characters')} characters; "
                f"{meta['tokens']} tokens (cl100k_base)"
            )
        if meta.get("block_count") is not None:
            click.echo(f"    Content blocks: {meta['block_count']}")
        if meta.get("source_id"):
            click.echo(f"    View: {meta.get('view_id', 'legacy')}")
            click.echo(f"    Source: {meta['source_id']}; revision: {meta['source_revision_id']}")
            click.echo(f"    {meta['status']}: {meta.get('message') or ''}")
            click.echo(
                f"    Processing: {meta.get('length_class') or 'unknown'} / "
                f"{meta.get('execution_mode') or 'unknown'}"
            )
            for unit in meta.get("units", []):
                click.echo(
                    f"    Unit {unit.get('name') or unit['unit_id']} ({unit['unit_id']}): "
                    f"{unit['status']}; {unit.get('length_class') or 'unknown'} / "
                    f"{unit.get('execution_mode') or 'unknown'}; actual: "
                    f"{unit.get('successful_source_revision_id') or 'none'}"
                )
            target = meta.get("target_processing")
            if target and target != meta.get("processing"):
                click.echo(
                    f"    Target processing: {target['length_class']} / "
                    f"{target['execution_mode']}; "
                    f"capacity: {target['capacity_status']}"
                )
            click.echo(
                f"    Validity: {meta.get('validity', 'current')}; "
                f"generation: {meta.get('source_generation', '?')}"
            )

    for section in ("summaries", "concepts", "entities", "reports"):
        pages = inventory[section]
        if pages:
            click.echo(f"\n{section.title()} ({len(pages)}):")
            for name in pages:
                click.echo(f"  - {name}")


def print_status(kb_dir: Path, *, scope: KnowledgeScope | None = None) -> None:
    from openkb.application.knowledge_bases import get_kb_status

    status = get_kb_status(kb_dir, scope=scope)
    click.echo(f"Knowledge base: {kb_dir}")
    for view_id, reasons in status["needs_refresh"].items():
        click.echo(f"  Needs refresh: {view_id} ({len(reasons)} pages)")
    click.echo("\nKnowledge Base Status:")
    click.echo(f"  {'Directory':<20} {'Files':<10}")
    click.echo(f"  {'-' * 20} {'-' * 10}")
    for directory, count in status["directories"].items():
        click.echo(f"  {directory:<20} {count:<10}")
    click.echo(f"  {'raw':<20} {status['raw_count']:<10}")
    click.echo(f"\n  Total indexed: {status['total_indexed']} document(s)")
    for key, label in (("last_compile", "Last compile"), ("last_lint", "Last lint")):
        if status[key]:
            click.echo(f"  {label}:  {status[key]}")
