"""Thin JSON commands for explicit bounded storage maintenance."""

import json
from dataclasses import asdict
from pathlib import Path

import click

from ultrakb.application.storage_maintenance import (
    archive_task_observations,
    audit_knowledge_usage,
    backup_knowledge,
    collect_garbage,
    preview_storage_maintenance,
    repair_knowledge_receipts,
    repair_knowledge_usage,
    restore_knowledge,
    retire_unused_roots,
)
from ultrakb.cli_shared import stored_kb_root
from ultrakb.storage.database import DatabaseIdentity


@click.group()
def storage():
    """Preview, explicitly maintain, back up and restore a main-store KB."""


def _limits(*, default_bytes=64 * 1024**2):
    def decorate(command):
        command = click.option("--max-bytes", type=click.IntRange(1), default=default_bytes)(
            command
        )
        return click.option("--max-items", type=click.IntRange(1, 1000), default=128)(command)

    return decorate


def _identity(command):
    command = click.option("--generation")(command)
    return click.option("--kb-id")(command)


def _run(ctx, operation, *args, kb_id=None, generation=None, **options):
    try:
        if (kb_id is None) != (generation is None):
            raise ValueError("Supply both --kb-id and --generation from the reviewed preview")
        identity = DatabaseIdentity(kb_id, generation) if kb_id is not None else None
        result = operation(stored_kb_root(ctx), *args, identity=identity, **options)
    except (ValueError, OSError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))


@storage.command("preview")
@_limits()
@click.pass_context
def preview(ctx, **options):
    """Show current protected and reclaimable content; preview authorizes no deletion."""
    _run(ctx, preview_storage_maintenance, **options)


@storage.command("retire-roots")
@_limits(default_bytes=256 * 1024)
@_identity
@click.pass_context
def retire(ctx, **options):
    """Release one bounded batch of obsolete checkpoints and sync-plan detail."""
    _run(ctx, retire_unused_roots, **options)


@storage.command("collect")
@_limits()
@_identity
@click.pass_context
def collect(ctx, **options):
    """Delete one bounded batch of currently unreferenced content and trees."""
    _run(ctx, collect_garbage, **options)


@storage.command("repair-receipts")
@_limits()
@_identity
@click.pass_context
def repair(ctx, **options):
    """Accept retained model responses and usage without sending requests again."""
    _run(ctx, repair_knowledge_receipts, **options)


@storage.command("backup")
@click.option("--output", required=True, type=click.Path(path_type=Path))
@_identity
@click.pass_context
def backup(ctx, output, **options):
    """Write a verified backup package to a new external directory."""
    _run(ctx, backup_knowledge, output, **options)


@storage.command("audit-usage")
@_limits(default_bytes=256 * 1024)
@_identity
@click.pass_context
def audit_usage(ctx, **options):
    """Compare cached usage counters with an explicit full scan of request records."""
    _run(ctx, audit_knowledge_usage, **options)


@storage.command("repair-usage")
@_limits(default_bytes=256 * 1024)
@_identity
@click.pass_context
def repair_usage(ctx, **options):
    """Rebuild one bounded counter batch without modifying requests or budgets."""
    _run(ctx, repair_knowledge_usage, **options)


@storage.command("restore")
@click.argument("backup", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--output", required=True, type=click.Path(path_type=Path))
def restore(backup, output):
    """Restore a verified package into a new directory and generation."""
    try:
        result = restore_knowledge(backup, output)
    except (ValueError, OSError, KeyError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(asdict(result), ensure_ascii=False, indent=2))


@storage.command("archive-observations")
@_limits(default_bytes=256 * 1024)
@_identity
@click.pass_context
def archive_observations(ctx, **options):
    """Archive one expired optional-detail batch, retaining core task records."""
    _run(ctx, archive_task_observations, **options)
