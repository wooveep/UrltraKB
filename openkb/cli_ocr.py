"""OCR setup adapters; importing documents never invokes these acquisition commands."""

import json
from pathlib import Path

import click

from openkb.application.ocr_installation import (
    export_ocr_package,
    install_ocr,
    prepare_ocr_install,
    read_ocr_installations,
)


@click.group("ocr")
def ocr_commands():
    """Prepare optional OCR packages, then select them in parsing settings."""


@ocr_commands.command("status")
def status():
    click.echo(json.dumps(read_ocr_installations(), ensure_ascii=False, indent=2))


@ocr_commands.command("prepare")
@click.argument("profile", type=click.Choice(["native", "nvidia", "openvino"]))
@click.option("--destination", type=click.Path(path_type=Path))
def prepare(profile, destination):
    """Show fixed versions, file hashes, exact download bytes and destination."""
    click.echo(json.dumps(prepare_ocr_install(profile, destination), ensure_ascii=False, indent=2))


@ocr_commands.command("install")
@click.argument("profile", type=click.Choice(["native", "nvidia", "openvino"]))
@click.option("--destination", type=click.Path(path_type=Path))
@click.option("--offline", type=click.Path(path_type=Path, exists=True))
def install(profile, destination, offline):
    """Explicitly acquire/install the selected package. Does not change OCR defaults."""
    click.echo(
        json.dumps(
            install_ocr(profile, destination=destination, offline=offline),
            ensure_ascii=False,
            indent=2,
        )
    )


@ocr_commands.command("export-package")
@click.argument("profile", type=click.Choice(["native", "nvidia", "openvino"]))
@click.argument("directory", type=click.Path(path_type=Path))
def export(profile, directory):
    click.echo(json.dumps(export_ocr_package(profile, directory), ensure_ascii=False, indent=2))


@ocr_commands.command("check")
@click.argument("installation")
@click.option("--device", type=click.Choice(["auto", "cpu", "gpu"]), default="auto")
@click.option("--gpu-device", help="Concrete device, for example GPU.1 or gpu:1.")
def check(installation, device, gpu_device):
    """Verify the complete selected pipeline using a built-in sample page."""
    from openkb.application.ocr_capability import check_ocr_capability

    click.echo(
        json.dumps(
            check_ocr_capability(installation, device, gpu_device=gpu_device),
            ensure_ascii=False,
            indent=2,
        )
    )


@ocr_commands.command("check-service")
@click.option("--kb", type=click.Path(path_type=Path, exists=True))
def check_service(kb):
    """Test the saved deployed service using an application-owned sample page."""
    from openkb.application.ocr_capability import check_ocr_service

    click.echo(json.dumps(check_ocr_service(kb), ensure_ascii=False, indent=2))


@ocr_commands.command("settings")
@click.option("--kb", type=click.Path(path_type=Path, exists=True))
def settings(kb):
    """Show effective policy, engine and configuration source without credentials."""
    from openkb.application.settings import read_settings_view

    view = read_settings_view(kb)
    click.echo(
        json.dumps(
            {
                "ocr": view.values.parsing.ocr.model_dump(),
                "source": view.sources["parsing"],
                "has_cloud_api_key": view.values.has_ocr_api_key,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@ocr_commands.command("configure")
@click.option("--kb", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--settings", "settings_file", type=click.Path(path_type=Path, exists=True), required=True
)
def configure(kb, settings_file):
    """Save a JSON OCR settings object through the shared settings interface."""
    from openkb.application.settings import apply_global_config_patch, apply_kb_config_patch
    from openkb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest

    values = {"parsing": {"ocr": json.loads(settings_file.read_text(encoding="utf-8"))}}
    result = (
        apply_kb_config_patch(kb, KbConfigPatchRequest(kb=str(kb), config=values))
        if kb
        else apply_global_config_patch(GlobalConfigPatchRequest(config=values))
    )
    click.echo(result.model_dump_json(indent=2))
