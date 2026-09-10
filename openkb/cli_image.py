"""Thin CLI adapter for independent image settings and explicit sample tests."""

import asyncio
import json
from pathlib import Path

import click

from openkb.application.image_understanding import test_image_connection
from openkb.application.settings import (
    apply_global_config_patch,
    apply_kb_config_patch,
    read_settings_view,
)
from openkb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest


@click.group("image")
def image_commands():
    """Configure and test optional image understanding."""


@image_commands.command("status")
@click.option("--kb", type=click.Path(path_type=Path, exists=True))
def status(kb):
    view = read_settings_view(kb)
    click.echo(
        json.dumps(
            {
                "settings": view.values.image_understanding.model_dump(),
                "source": view.sources["image_understanding"],
                "has_api_key": view.values.has_image_api_key,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@image_commands.command("configure")
@click.option("--kb", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--settings",
    type=click.Path(path_type=Path, exists=True),
    help="JSON image-understanding settings (contains no API key).",
)
@click.option("--set-key", is_flag=True, help="Read a replacement key from a hidden prompt.")
@click.option("--clear-key", is_flag=True)
@click.option("--clear-override", is_flag=True)
def configure(kb, settings, set_key, clear_key, clear_override):
    if (set_key and clear_key) or (settings and clear_override):
        raise click.UsageError("Choose either setting or clearing the same field.")
    config = {}
    if settings:
        config["image_understanding"] = json.loads(settings.read_text(encoding="utf-8"))
    elif clear_override:
        config["image_understanding"] = None
    patch = {"config": config}
    if set_key or clear_key:
        patch["image_api_key"] = click.prompt("Image API key", hide_input=True) if set_key else None
    if kb:
        result = apply_kb_config_patch(kb, KbConfigPatchRequest(kb=str(kb), **patch))
    else:
        result = apply_global_config_patch(GlobalConfigPatchRequest(**patch))
    click.echo(result.model_dump_json(indent=2))


@image_commands.command("test")
@click.option("--kb", type=click.Path(path_type=Path, exists=True))
def test(kb):
    """Send the built-in sample to the saved image connection. Does not enable it."""
    click.echo(json.dumps(asyncio.run(test_image_connection(kb)), ensure_ascii=False, indent=2))
