"""Explicit bounded pending-work drains; no implicit background service."""

import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import click

from ultrakb.application.pending import (
    cancel_execution_group,
    pending_status,
    process_pending,
    read_pending_job,
    retry_pending_job,
    update_execution_budget,
)
from ultrakb.cli_shared import stored_kb_root


@click.command("process-pending")
@click.option("--kb", "path", type=click.Path(path_type=Path, file_okay=False))
@click.option("--max-jobs", default=10000, type=click.IntRange(1, 10000))
@click.pass_context
def process(ctx, path, max_jobs):
    """Drain runnable discoveries/imports and exit; waiting/unknown jobs remain visible."""
    from ultrakb.cli import _selected_scope

    try:
        root = path or stored_kb_root(ctx)
        with redirect_stdout(sys.stderr):
            result = process_pending(root, max_jobs=max_jobs, scope=_selected_scope(ctx, root))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@click.group("pending")
def pending():
    """Inspect pending work, retry unknown work explicitly, or cancel an execution group."""


@pending.command("list")
@click.option("--after", default=0, type=click.IntRange(0, 2**63 - 1))
@click.option("--limit", default=63, type=click.IntRange(1, 128))
@click.pass_context
def inventory(ctx, after, limit):
    from ultrakb.cli import _selected_scope

    root = stored_kb_root(ctx)
    click.echo(
        json.dumps(
            pending_status(root, scope=_selected_scope(ctx, root), after=after, limit=limit),
            ensure_ascii=False,
            indent=2,
        )
    )


@pending.command("show")
@click.argument("job_id")
@click.option("--after-object", default=0, type=click.IntRange(0, 2**63 - 1))
@click.option("--limit", default=63, type=click.IntRange(1, 128))
@click.pass_context
def detail(ctx, job_id, after_object, limit):
    from ultrakb.cli import _selected_scope

    root = stored_kb_root(ctx)
    try:
        value = read_pending_job(
            root,
            job_id,
            scope=_selected_scope(ctx, root),
            checkpoint_after=after_object,
            limit=limit,
        )
    except (OSError, ValueError, KeyError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(json.dumps(value, ensure_ascii=False, indent=2))


@pending.command("retry")
@click.argument("job_id")
@click.pass_context
def retry(ctx, job_id):
    from ultrakb.cli import _selected_scope

    try:
        root = stored_kb_root(ctx)
        retry_pending_job(root, job_id, scope=_selected_scope(ctx, root))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Retry queued; run process-pending to execute.")


@pending.command("cancel-group")
@click.argument("group_id")
@click.pass_context
def cancel(ctx, group_id):
    from ultrakb.cli import _selected_scope

    try:
        root = stored_kb_root(ctx)
        cancel_execution_group(root, group_id, scope=_selected_scope(ctx, root))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("Execution group cancelled; published documents retained.")


@pending.command("budget")
@click.argument("group_id")
@click.argument("limits", nargs=-1, required=True)
@click.pass_context
def budget(ctx, group_id, limits):
    """Set LIMIT=VALUE, e.g. max_sources=200 or max_discovery_seconds=60."""
    from ultrakb.cli import _selected_scope

    try:
        patch = dict(item.split("=", 1) for item in limits)
        parsed = {
            key: float(value) if key == "max_discovery_seconds" else int(value)
            for key, value in patch.items()
        }
        root = stored_kb_root(ctx)
        result = update_execution_budget(root, group_id, parsed, scope=_selected_scope(ctx, root))
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))
