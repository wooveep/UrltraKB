"""Refresh selected current knowledge and review protected candidate differences."""

import asyncio
import json
from dataclasses import asdict

import click


def _selection(ctx, *, required=True):
    from openkb.cli import _selected_scope
    from openkb.cli_views import _root

    root = _root(ctx)
    scope = _selected_scope(ctx, root)
    if required and scope is None:
        raise click.ClickException("Choose the knowledge view with --view")
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
    from openkb.application.refresh import refresh_status

    root, scope = _selection(ctx)
    click.echo(json.dumps(refresh_status(root, scope=scope), ensure_ascii=False))


@refresh.command("run")
@click.pass_context
def run(ctx):
    from openkb.application.refresh import refresh_knowledge_view

    root, scope = _selection(ctx)
    _emit(asyncio.run(refresh_knowledge_view(root, scope=scope)))


@refresh.command("empty")
@click.argument("source_id")
@click.option("--generation", type=int, required=True)
@click.pass_context
def empty(ctx, source_id, generation):
    """Explicitly confirm that this source revision makes no current contribution."""
    from openkb.application.refresh import confirm_empty_source

    root, scope = _selection(ctx, required=False)
    _emit(confirm_empty_source(root, source_id, generation=generation, scope=scope))


@refresh.command("proposals")
@click.pass_context
def proposals(ctx):
    from openkb.application.refresh import list_refresh_proposals

    root, scope = _selection(ctx, required=False)
    click.echo(json.dumps(list_refresh_proposals(root, scope=scope), ensure_ascii=False))


@refresh.command("show")
@click.argument("proposal_id")
@click.pass_context
def show(ctx, proposal_id):
    from openkb.application.refresh import read_refresh_proposal

    root, scope = _selection(ctx, required=False)
    click.echo(
        json.dumps(read_refresh_proposal(root, proposal_id, scope=scope), ensure_ascii=False)
    )


@refresh.command("accept")
@click.argument("proposal_id")
@click.option("--version", required=True, help="Version from the reviewed saved diff.")
@click.pass_context
def accept(ctx, proposal_id, version):
    from openkb.application.refresh import accept_refresh_proposal

    root, scope = _selection(ctx, required=False)
    _emit(accept_refresh_proposal(root, proposal_id, version=version, scope=scope))


@refresh.command("history")
@click.pass_context
def history(ctx):
    from openkb.application.refresh import list_knowledge_history

    root, scope = _selection(ctx)
    click.echo(json.dumps(list_knowledge_history(root, scope=scope), ensure_ascii=False))


@refresh.command("read-history")
@click.argument("knowledge_revision_id")
@click.argument("page")
@click.pass_context
def read_history(ctx, knowledge_revision_id, page):
    from openkb.application.pages import read_page
    from openkb.application.views import view_scope

    root, scope = _selection(ctx)
    selected = view_scope(root, scope.view_id, historical_revision=knowledge_revision_id)
    click.echo(json.dumps(asdict(read_page(root, page, scope=selected)), ensure_ascii=False))


@refresh.command("references")
@click.pass_context
def references(ctx):
    from openkb.artifact_references import list_artifact_references

    root, _ = _selection(ctx, required=False)
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
    from openkb.application.refresh import cleanup_unreferenced_artifacts

    root, _ = _selection(ctx, required=False)
    click.echo(json.dumps(cleanup_unreferenced_artifacts(root, list(paths)), ensure_ascii=False))
