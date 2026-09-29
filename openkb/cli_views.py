"""Explicit knowledge view inventory and evidence-only legacy mapping."""

import json

import click


@click.group()
def views():
    """List knowledge views; select one with the global --view option."""


def _root(ctx):
    from openkb.cli import _find_kb_dir

    root = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if root is None:
        raise click.ClickException("No knowledge base found")
    return root


@views.command("list")
@click.pass_context
def list_views_command(ctx):
    from openkb.application.views import list_views

    for view in list_views(_root(ctx)):
        click.echo(json.dumps(view.model_dump(mode="json"), ensure_ascii=False))


@views.command("map-legacy")
@click.pass_context
def map_legacy_command(ctx):
    """Map surviving legacy originals without labeling or recompiling old wiki pages."""
    from openkb.application.views import map_legacy_sources

    click.echo(json.dumps(map_legacy_sources(_root(ctx)), ensure_ascii=False))
