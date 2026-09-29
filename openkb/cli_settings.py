"""Read effective settings and their origins through the shared application seam."""

import click


@click.command()
@click.option("--global", "global_settings", is_flag=True, help="Read global defaults.")
@click.pass_context
def settings(ctx, global_settings):
    """Show effective configuration and provenance as JSON (without API keys)."""
    from openkb.application.settings import read_settings_view
    from openkb.cli import _find_kb_dir

    root = None if global_settings else _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if root is None and not global_settings:
        raise click.ClickException("No knowledge base found. Use --global for global defaults.")
    click.echo(read_settings_view(root).model_dump_json(indent=2))
