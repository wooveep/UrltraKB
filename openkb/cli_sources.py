"""CLI access to saved sources and version-bound continuation."""

from __future__ import annotations

import json

import click

from openkb.application.source_actions import review_source_proposal
from openkb.application.source_history import source_status
from openkb.cli_import import run_requests
from openkb.runtime.requests import (
    CleanupSourceHistory,
    ConfirmSourcePage,
    ContinueSource,
    ReparseSource,
    ReprocessSourcePage,
)


def _root(ctx):
    from openkb.cli import _find_kb_dir

    root = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if root is None:
        raise click.ClickException("No knowledge base found. Run openkb init first.")
    return root


@click.group("source")
def sources():
    """Review retained source versions and continue unfinished knowledge work."""


@sources.command("status")
@click.argument("source_id")
@click.pass_context
def status(ctx, source_id):
    """Show intake, compilation and cumulative usage for one source."""
    try:
        click.echo(json.dumps(source_status(_root(ctx), source_id), ensure_ascii=False, indent=2))
    except (ValueError, OSError) as exc:
        raise click.ClickException("Source is unavailable") from exc


@sources.command("cleanup-preview")
@click.pass_context
def cleanup_preview(ctx):
    """List unreferenced history and space that an explicit cleanup can release."""
    from dataclasses import asdict

    from openkb.application.source_cleanup import preview_history_cleanup

    click.echo(
        json.dumps(asdict(preview_history_cleanup(_root(ctx))), ensure_ascii=False, indent=2)
    )


@sources.command("cleanup")
@click.option("--preview", "preview_id", required=True, help="Exact cleanup preview you reviewed.")
@click.pass_context
def cleanup(ctx, preview_id):
    """Delete exactly the reviewed unreferenced history, preserving cited versions."""
    try:
        unit = CleanupSourceHistory(preview_id)
    except ValueError as exc:
        raise click.ClickException("Use the identity from the reviewed cleanup preview") from exc
    ctx.exit(run_requests(_root(ctx), [unit]))


@sources.command("review")
@click.argument("proposal_id")
@click.option("--page", help="Review one exact Wiki path from this proposal.")
@click.option("--max-chars", type=click.IntRange(1, 1_000_000), default=100_000)
@click.pass_context
def review(ctx, proposal_id, page, max_chars):
    """Show the saved difference before accepting protected page changes."""
    try:
        result = review_source_proposal(_root(ctx), proposal_id, page=page, max_chars=max_chars)
        click.echo(f"Proposal: {proposal_id}")
        for name in result["protected"]:
            click.echo(f"Requires acceptance: {name}")
        for difference in result["diffs"].values():
            click.echo(difference)
    except (ValueError, OSError) as exc:
        raise click.ClickException("Proposal is unavailable or exceeds the review limit") from exc


@sources.command("continue")
@click.argument("source_id")
@click.option("--version", "version_id", required=True, help="Reviewed source version.")
@click.option("--proposal", "proposal_id", help="Saved proposal; reuses completed model work.")
@click.option(
    "--accept-page", multiple=True, help="Exact reviewed protected Wiki path; repeat per page."
)
@click.pass_context
def continue_saved(ctx, source_id, version_id, proposal_id, accept_page):
    """Continue saved input, or accept exactly the reviewed proposal pages."""
    try:
        unit = ContinueSource(source_id, version_id, proposal_id, accept_page or None)
    except ValueError as exc:
        raise click.ClickException(
            "Use the source, version and proposal identities shown in review"
        ) from exc
    ctx.exit(run_requests(_root(ctx), [unit]))


@sources.command("reprocess-page")
@click.argument("source_id")
@click.option("--version", "version_id", required=True)
@click.option("--parse", "parse_id", required=True)
@click.option("--page", type=click.IntRange(1), required=True)
@click.option(
    "--acknowledge-unknown",
    is_flag=True,
    help="Allow a new submission despite an unknown prior cloud outcome; "
    "duplicate charges are possible.",
)
@click.pass_context
def reprocess_page(ctx, source_id, version_id, parse_id, page, acknowledge_unknown):
    """Run the selected OCR backend again for one reviewed physical page."""
    try:
        unit = ReprocessSourcePage(source_id, version_id, parse_id, page, acknowledge_unknown)
    except ValueError as exc:
        raise click.ClickException("Invalid source page reprocessing decision") from exc
    ctx.exit(run_requests(_root(ctx), [unit]))


@sources.command("reparse")
@click.argument("source_id")
@click.option("--version", "version_id", required=True)
@click.pass_context
def reparse(ctx, source_id, version_id):
    """Reparse retained input with current settings and reusable OCR results."""
    try:
        unit = ReparseSource(source_id, version_id)
    except ValueError as exc:
        raise click.ClickException("Invalid source or version identity") from exc
    ctx.exit(run_requests(_root(ctx), [unit]))


@sources.command("confirm-page")
@click.argument("source_id")
@click.option("--version", "version_id", required=True)
@click.option("--parse", "parse_id", required=True)
@click.option("--page", type=click.IntRange(1), required=True)
@click.option(
    "--reason", type=click.Choice(["legitimate_blank", "legitimate_illustration"]), required=True
)
@click.pass_context
def confirm_page(ctx, source_id, version_id, parse_id, page, reason):
    """Record an explicit decision after viewing the original physical page."""
    try:
        unit = ConfirmSourcePage(source_id, version_id, parse_id, page, reason)
    except ValueError as exc:
        raise click.ClickException("Invalid source page decision") from exc
    ctx.exit(run_requests(_root(ctx), [unit]))
