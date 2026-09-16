"""Artifact inspection, explicit deletion and portable export commands."""

import json
from pathlib import Path

import click

from openkb.application.artifacts import (
    artifact_quality,
    delete_artifact,
    export_artifact,
    list_artifacts,
)
from openkb.artifact_presentation import quality_summary


def _kb():
    from openkb.cli import _find_kb_dir

    root = _find_kb_dir()
    if root is None:
        raise click.ClickException("No knowledge base found")
    return root


@click.group("artifact")
def artifacts():
    """Inspect, export or delete saved artifacts and their evidence records."""


@artifacts.command("list")
def listing():
    root = _kb()
    for item in list_artifacts(root):
        click.echo(f"{item.path}\n  {quality_summary(artifact_quality(root, item.path))}")


@artifacts.command("quality")
@click.argument("path")
def quality(path):
    click.echo(json.dumps(artifact_quality(_kb(), path), ensure_ascii=False, indent=2))


@artifacts.command("export")
@click.argument("path")
@click.argument("destination", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--include-evidence", is_flag=True, help="Include cited original ranges and images.")
def export(path, destination, include_evidence):
    click.echo(export_artifact(_kb(), path, destination, include_evidence=include_evidence))


@artifacts.command("delete")
@click.argument("path")
@click.confirmation_option(prompt="Delete this artifact and its evidence retention record?")
def delete(path):
    delete_artifact(_kb(), path)
    click.echo(f"Deleted: {path}")
