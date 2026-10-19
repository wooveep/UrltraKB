"""Refresh selected current knowledge and review protected candidate differences."""

import asyncio
import json
from dataclasses import asdict

import click


def _selection(ctx):
    from ultrakb.cli import _require_kb_root as _root
    from ultrakb.cli import _selected_scope

    root = _root(ctx)
    scope = _selected_scope(ctx, root)
    return root, scope


def _emit(result):
    click.echo(json.dumps(asdict(result), ensure_ascii=False))
    if result.status in {"failed", "conflict", "blocked"}:
        raise click.ClickException(result.message)


@click.group()
def refresh():
    """Refresh current evidence after source updates, empty confirmation or withdrawal."""


@refresh.command("status")
@click.pass_context
def status(ctx):
    from ultrakb.application.refresh import refresh_status

    root, scope = _selection(ctx)
    click.echo(json.dumps(refresh_status(root, scope=scope), ensure_ascii=False))


@refresh.command("run")
@click.option(
    "--source", "sources", multiple=True, help="Refresh these accepted source contributions."
)
@click.option(
    "--page", "pages", multiple=True, help="Limit selected-source refresh to pending pages."
)
@click.pass_context
def run(ctx, sources, pages):
    from ultrakb.application.refresh import refresh_knowledge_view, refresh_selected_sources

    root, scope = _selection(ctx)
    if pages and not sources:
        raise click.UsageError("--page requires at least one --source")
    _emit(
        asyncio.run(
            refresh_selected_sources(
                root, list(sources), scope=scope, page_paths=list(pages) if pages else None
            )
            if sources
            else refresh_knowledge_view(root, scope=scope)
        )
    )


@refresh.command("empty")
@click.argument("source_id")
@click.option("--generation", type=int, required=True)
@click.pass_context
def empty(ctx, source_id, generation):
    """Explicitly confirm that this source revision makes no current contribution."""
    from ultrakb.application.refresh import confirm_empty_source

    root, scope = _selection(ctx)
    _emit(confirm_empty_source(root, source_id, generation=generation, scope=scope))


@refresh.command("proposals")
@click.pass_context
def proposals(ctx):
    from ultrakb.application.refresh import list_refresh_proposals

    root, scope = _selection(ctx)
    click.echo(json.dumps(list_refresh_proposals(root, scope=scope), ensure_ascii=False))


@refresh.command("show")
@click.argument("proposal_id")
@click.pass_context
def show(ctx, proposal_id):
    from ultrakb.application.refresh import read_refresh_proposal

    root, scope = _selection(ctx)
    click.echo(
        json.dumps(read_refresh_proposal(root, proposal_id, scope=scope), ensure_ascii=False)
    )


@refresh.command("accept")
@click.argument("proposal_id")
@click.option("--version", required=True, help="Version from the reviewed saved diff.")
@click.pass_context
def accept(ctx, proposal_id, version):
    from ultrakb.application.refresh import accept_refresh_proposal

    root, scope = _selection(ctx)
    _emit(accept_refresh_proposal(root, proposal_id, version=version, scope=scope))


@refresh.command("history")
@click.option("--after", default=None)
@click.option("--limit", type=click.IntRange(1, 128), default=63)
@click.option("--publication-id", default=None)
@click.pass_context
def history(ctx, after, limit, publication_id):
    from ultrakb.application.refresh import list_knowledge_history

    root, scope = _selection(ctx)
    click.echo(
        json.dumps(
            list_knowledge_history(
                root, scope=scope, after=after, limit=limit, publication_id=publication_id
            ),
            ensure_ascii=False,
        )
    )


@refresh.command("read-history")
@click.argument("knowledge_revision_id")
@click.argument("page")
@click.pass_context
def read_history(ctx, knowledge_revision_id, page):
    from ultrakb.application.knowledge_history import read_history_page

    root, scope = _selection(ctx)
    value, _ = read_history_page(root, knowledge_revision_id, page, scope=scope)
    click.echo(json.dumps(asdict(value), ensure_ascii=False))


@refresh.command("references")
@click.pass_context
def references(ctx):
    from ultrakb.artifact_references import list_artifact_references

    root, _ = _selection(ctx)
    found = list_artifact_references(root)
    click.echo(
        json.dumps(
            {
                "complete": found.complete,
                "paths": [str(p) for p in sorted(found.paths)],
                "index_documents": sorted(found.index_documents),
            },
            ensure_ascii=False,
        )
    )


@refresh.command("cleanup")
@click.argument("paths", nargs=-1, required=True)
@click.pass_context
def cleanup(ctx, paths):
    """Delete selected managed artifacts only if a complete inventory proves no references."""
    from ultrakb.application.refresh import cleanup_unreferenced_artifacts

    root, _ = _selection(ctx)
    click.echo(json.dumps(cleanup_unreferenced_artifacts(root, list(paths)), ensure_ascii=False))
