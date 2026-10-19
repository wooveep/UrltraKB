"""Thin CLI adapters over bounded authoritative task queries."""

from pathlib import Path

import click

from ultrakb.application.knowledge_tasks import (
    export_knowledge_stage,
    export_knowledge_task,
    list_knowledge_tasks,
    read_knowledge_stage,
    read_knowledge_task,
)


@click.group()
def tasks():
    """Read main-store task history and export a bounded detail page."""


def _detail_options(command):
    for name in ("executions", "stages", "requests", "publications"):
        command = click.option(f"--{name}-after", type=click.IntRange(0, 2**63 - 1), default=0)(
            command
        )
    command = click.option("--execution-id")(command)
    return click.option("--limit", type=click.IntRange(1, 128), default=63)(command)


def _run(ctx, operation, *args, **options):
    from ultrakb.cli_shared import stored_kb_root

    try:
        result = operation(stored_kb_root(ctx), *args, **options)
    except (ValueError, OSError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(result.model_dump_json(indent=2))


@tasks.command("list")
@click.option("--after", type=click.IntRange(0, 2**63 - 1), default=0)
@click.option("--limit", type=click.IntRange(1, 128), default=63)
@click.pass_context
def list_tasks(ctx, **options):
    """List task summaries, using next_cursor for the next page."""
    _run(ctx, list_knowledge_tasks, **options)


@tasks.command("show")
@click.argument("task_id")
@_detail_options
@click.pass_context
def show_task(ctx, task_id, **options):
    """Show one task page, with independent detail cursors."""
    _run(ctx, read_knowledge_task, task_id, **options)


@tasks.command("export")
@click.argument("task_id")
@click.option("--output", type=click.Path(path_type=Path), required=True)
@_detail_options
@click.pass_context
def export_task(ctx, task_id, output, **options):
    """Write the same bounded detail schema to an external JSON file."""
    _run(ctx, export_knowledge_task, task_id, output, **options)


@tasks.command("stage")
@click.argument("task_id")
@click.argument("stage_id")
@click.option("--requests-after", type=click.IntRange(0, 2**63 - 1), default=0)
@click.option("--limit", type=click.IntRange(1, 128), default=63)
@click.option("--output", type=click.Path(path_type=Path))
@click.pass_context
def stage_task(ctx, task_id, stage_id, output, **options):
    """Read a stage, retained input and exact request page, or export its JSON."""
    if output is None:
        _run(ctx, read_knowledge_stage, task_id, stage_id, **options)
    else:
        _run(ctx, export_knowledge_stage, task_id, stage_id, output, **options)
