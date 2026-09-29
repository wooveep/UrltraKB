"""Read retained source bodies and physical page ranges at an explicit revision."""

import json

import click


@click.command("source")
@click.argument("source_id")
@click.option("--pages", help="Physical pages, e.g. 1,3-5; omit to read the complete source.")
@click.option("--chars", help="Unicode character range START:END (0-based, end exclusive).")
@click.option("--blocks", help="Content blocks, e.g. 1,3-5 (not physical pages).")
@click.option("--source-revision", help="Read this frozen source revision.")
@click.option("--knowledge-revision", help="Read a historical knowledge revision in --view.")
@click.pass_context
def source(ctx, source_id, pages, chars, blocks, source_revision, knowledge_revision):
    """Read SOURCE_ID (from list) with revision and coverage information."""
    from openkb.application.views import view_scope
    from openkb.cli import _selected_scope
    from openkb.cli_views import _root
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
            source_revision_id=source_revision,
            scope=scope,
        )
        if result is None:
            raise ValueError("Document source not found")
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
