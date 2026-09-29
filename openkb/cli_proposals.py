"""Explicit review actions for durable knowledge proposals."""

import click

from openkb.application.proposals import accept_proposal, list_proposals, read_proposal
from openkb.ingest_result import describe_ingest


def _root(ctx):
    from openkb.cli import _find_kb_dir

    root = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if root is None:
        raise click.ClickException("No knowledge base found. Run openkb init first.")
    return root


def _scope(ctx):
    from openkb.cli import _selected_scope

    return _selected_scope(ctx, _root(ctx))


@click.group("proposals")
def proposals():
    """Review and accept saved knowledge differences."""


@proposals.command("list")
@click.pass_context
def list_pending(ctx):
    for item in list_proposals(_root(ctx), scope=_scope(ctx)):
        click.echo(
            f"{item.proposal_id}  view={item.view_id}  source={item.source_id}  {item.status}"
        )


@proposals.command("show")
@click.argument("proposal_id")
@click.pass_context
def show(ctx, proposal_id):
    item = read_proposal(_root(ctx), proposal_id, scope=_scope(ctx))
    click.echo(
        f"Proposal {item.proposal_id}\nView: {item.view_id}\nVersion: {item.version}\n{item.diff}"
    )


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
