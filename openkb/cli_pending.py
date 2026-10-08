"""Explicit bounded pending-work drains; no implicit background service."""

import json
from pathlib import Path

import click

from openkb.application.pending import (
    cancel_execution_group,
    pending_status,
    process_pending,
    retry_pending_job,
    update_execution_budget,
)


@click.command("process-pending")
@click.option("--kb", "path", type=click.Path(path_type=Path, file_okay=False))
@click.option("--max-jobs", default=10000, type=click.IntRange(1, 10000))
@click.pass_context
def process(ctx, path, max_jobs):
    """Drain runnable discoveries/imports and exit; waiting/unknown jobs remain visible."""
    from openkb.cli import _require_kb_root as _root

    try:
        result = process_pending(path or _root(ctx), max_jobs=max_jobs)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@click.group("pending")
def pending():
    """Inspect pending work, retry unknown work explicitly, or cancel an execution group."""


@pending.command("list")
@click.pass_context
def inventory(ctx):
    from openkb.cli import _require_kb_root as _root

    click.echo(json.dumps(pending_status(_root(ctx)), ensure_ascii=False, indent=2))


@pending.command("retry")
@click.argument("job_id")
@click.pass_context
def retry(ctx, job_id):
    from openkb.cli import _require_kb_root as _root

    try:
        retry_pending_job(_root(ctx), job_id)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Retry queued; run process-pending to execute.")


@pending.command("cancel-group")
@click.argument("group_id")
@click.pass_context
def cancel(ctx, group_id):
    from openkb.cli import _require_kb_root as _root

    try:
        cancel_execution_group(_root(ctx), group_id)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Execution group cancelled; published documents retained.")


@pending.command("budget")
@click.argument("group_id")
@click.argument("limits", nargs=-1, required=True)
@click.pass_context
def budget(ctx, group_id, limits):
    """Set LIMIT=VALUE, e.g. max_sources=200 or max_discovery_seconds=60."""
    from openkb.cli import _require_kb_root as _root

    try:
        patch = dict(item.split("=", 1) for item in limits)
        parsed = {
            key: float(value) if key == "max_discovery_seconds" else int(value)
            for key, value in patch.items()
        }
        result = update_execution_budget(_root(ctx), group_id, parsed)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
