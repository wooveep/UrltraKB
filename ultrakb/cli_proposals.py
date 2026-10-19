"""Explicit review actions for durable knowledge proposals."""

import click

from ultrakb.application.proposals import accept_proposal, list_proposal_page, read_proposal
from ultrakb.ingest_result import describe_ingest


def _root(ctx):
    from ultrakb.cli import _find_kb_dir

    root = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if root is None:
        raise click.ClickException("No knowledge base found. Run ultrakb init first.")
    return root


def _scope(ctx):
    from ultrakb.cli import _selected_scope

    return _selected_scope(ctx, _root(ctx))


@click.group("proposals")
def proposals():
    """Review and accept saved knowledge differences."""


@proposals.command("list")
@click.option("--after", default=None, help="Continue after the cursor printed by a previous page.")
@click.option("--limit", default=63, type=click.IntRange(1, 128))
@click.pass_context
def list_pending(ctx, after, limit):
    try:
        page = list_proposal_page(_root(ctx), scope=_scope(ctx), after=after, limit=limit)
    except (ValueError, FileNotFoundError) as error:
        raise click.ClickException(str(error)) from error
    for item in page.proposals:
        click.echo(f"{item.proposal_id}  source={item.source_id}  {item.status}")
    if page.next_after:
        click.echo(f"Next cursor: {page.next_after}; continue with --after {page.next_after}")


@proposals.command("show")
@click.argument("proposal_id")
@click.pass_context
def show(ctx, proposal_id):
    item = read_proposal(_root(ctx), proposal_id, scope=_scope(ctx))
    click.echo(f"Proposal {item.proposal_id}\nVersion: {item.version}\n{item.diff}")


@proposals.command("accept")
@click.argument("proposal_id")
@click.option("--version", required=True, help="Version printed by proposals show.")
@click.pass_context
def accept(ctx, proposal_id, version):
    result = accept_proposal(_root(ctx), proposal_id, version=version, scope=_scope(ctx))
    for line in describe_ingest(result):
        click.echo(line)
    if result.status not in {"added", "skipped"}:
        ctx.exit(1)
