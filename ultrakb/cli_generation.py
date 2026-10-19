"""Render shared generation receipts without claiming a pending copy is ready."""

import json

import click

from ultrakb.application.generation_results import generation_receipt
from ultrakb.llm_usage import describe_model_usage


def present_generation(result, *, json_output):
    if json_output:
        click.echo(json.dumps(generation_receipt(result), ensure_ascii=False))
        return True
    if result.model_usage is not None:
        for line in describe_model_usage(result.model_usage):
            click.echo(line)
    if result.status != "completed" or result.projection_pending:
        for path in result.resources:
            click.echo(f"Accepted: {path}")
        if result.archive_path is not None:
            click.echo(f"Previous version retained: {result.archive_path}")
        for note in result.quality:
            click.echo(f"[WARN] {note}")
        for stage in result.unfinished:
            click.echo(f"Unfinished: {stage}")
        if result.validation is not None:
            for error in result.validation.errors:
                click.echo(f"[ERROR] {error}")
            for warning in result.validation.warnings:
                click.echo(f"[WARN] {warning}")
        if result.projection_pending:
            click.echo("Accepted in the main store; working-copy synchronization is pending.")
    return result.status == "completed" and result.projection_pending
