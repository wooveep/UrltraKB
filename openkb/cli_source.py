"""Read retained source bodies and physical page ranges at an explicit revision."""

import json

import click


@click.command("source")
@click.argument("source_id")
@click.option("--unit", "unit_id", help="Worksheet processing unit ID from list.")
@click.option("--cells", help="Disjoint worksheet cell ranges, e.g. A1:B3,D7.")
@click.option("--pages", help="Physical pages, e.g. 1,3-5; omit to read the complete source.")
@click.option("--chars", help="Unicode character range START:END (0-based, end exclusive).")
@click.option("--blocks", help="Content blocks, e.g. 1,3-5 (not physical pages).")
@click.option(
    "--part", type=click.Choice(["body", "notes"]), help="Read only slide body or speaker notes."
)
@click.option("--source-revision", help="Read this frozen source revision.")
@click.option("--knowledge-revision", help="Read a historical knowledge revision in --view.")
@click.pass_context
def source(
    ctx, source_id, pages, chars, blocks, part, source_revision, knowledge_revision, unit_id, cells
):
    """Read SOURCE_ID (from list) with revision and coverage information."""
    from openkb.application.views import view_scope
    from openkb.cli import _require_kb_root as _root
    from openkb.cli import _selected_scope
    from openkb.documents import read_document_source

    root = _root(ctx)
    try:
        scope = _selected_scope(ctx, root)
        if knowledge_revision:
            scope = view_scope(
                root, ctx.obj.get("view_id") or "legacy", historical_revision=knowledge_revision
            )
        result = read_document_source(
            root,
            source_id,
            pages=pages,
            chars=chars,
            blocks=blocks,
            part=part,
            source_revision_id=source_revision,
            unit_id=unit_id,
            cells=cells,
            scope=scope,
        )
        if result is None:
            raise ValueError("Document source not found")
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@click.command("retry-worksheet")
@click.argument("source_id")
@click.option("--unit", "unit_id", required=True, help="Retry this worksheet unit ID.")
@click.pass_context
def retry_worksheet(ctx, source_id, unit_id):
    from dataclasses import asdict

    from openkb.application.workbook_actions import retry_worksheet as retry
    from openkb.cli import _require_kb_root as _root
    from openkb.cli import _selected_scope

    root = _root(ctx)
    try:
        result = retry(root, source_id, unit_id, scope=_selected_scope(ctx, root))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))


@click.command("reprocess")
@click.argument("source_id")
@click.option(
    "--execute", metavar="PREVIEW_VERSION", help="Execute exactly this reviewed preview token."
)
@click.pass_context
def reprocess(ctx, source_id, execute):
    """Preview policy and originals; use --execute to create a new processing revision."""
    from dataclasses import asdict

    from openkb.application.reprocessing import preview_reprocessing, reprocess_source
    from openkb.cli import _require_kb_root as _root
    from openkb.cli import _selected_scope

    root = _root(ctx)
    try:
        scope = _selected_scope(ctx, root)
        value = (
            asdict(reprocess_source(root, source_id, version=execute, scope=scope))
            if execute
            else preview_reprocessing(root, source_id, scope=scope)
        )
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))


@click.command("retry-source")
@click.argument("source_id")
@click.pass_context
def retry_source(ctx, source_id):
    """Resume unfinished units from the retained input of SOURCE_ID."""
    from dataclasses import asdict

    from openkb.application.source_retry import retry_source as retry
    from openkb.cli import _require_kb_root as _root
    from openkb.cli import _selected_scope

    root = _root(ctx)
    try:
        result = retry(root, source_id, scope=_selected_scope(ctx, root))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))
