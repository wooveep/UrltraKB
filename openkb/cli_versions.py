"""Review provenance and explicitly continue sources waiting for version metadata."""

import click

from openkb.application.version_review import (
    cancel_version_review,
    list_version_reviews,
    read_version_review,
    resume_version_review,
    review_source_version,
    supplement_version_reviews,
)
from openkb.ingest_result import describe_ingest
from openkb.view_records import SourceMetadata


def _selection(ctx):
    from openkb.cli import _find_kb_dir, _selected_scope

    root = _find_kb_dir(ctx.obj.get("kb_dir_override"))
    if root is None:
        raise click.ClickException("No knowledge base found")
    return root, _selected_scope(ctx, root)


@click.group("versions")
def versions():
    """Inspect, supplement, cancel and resume version clarifications."""


@versions.command("open")
@click.argument("source_id")
@click.pass_context
def open_review(ctx, source_id):
    """Start a metadata correction for an already imported source."""
    root, scope = _selection(ctx)
    try:
        result = review_source_version(root, source_id, scope=scope)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(result.model_dump_json(indent=2))


@versions.command("list")
@click.pass_context
def list_pending(ctx):
    root, scope = _selection(ctx)
    for review in list_version_reviews(root, scope=scope):
        click.echo(
            f"{review.review_id}  {review.name}  {review.status}  "
            f"{', '.join(review.missing_fields)}"
        )
        for related in review.related_sources:
            click.echo(
                f"  Related: {related.name}; {related.metadata.product or 'unknown product'}; "
                f"{', '.join(related.metadata.applicable_versions) or 'unknown version'}; "
                f"{related.metadata.family or 'unknown purpose'}"
            )


@versions.command("show")
@click.argument("review_id")
@click.pass_context
def show(ctx, review_id):
    root, scope = _selection(ctx)
    click.echo(read_version_review(root, review_id, scope=scope).model_dump_json(indent=2))


@versions.command("supplement")
@click.argument("review_ids", nargs=-1, required=True)
@click.option(
    "--metadata",
    required=True,
    help="JSON fields to apply to every selected source; omitted fields stay unchanged.",
)
@click.pass_context
def supplement(ctx, review_ids, metadata):
    root, scope = _selection(ctx)
    try:
        patch = SourceMetadata.model_validate_json(metadata)
        results = supplement_version_reviews(root, dict.fromkeys(review_ids, patch), scope=scope)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    for review in results:
        click.echo(
            f"{review.review_id}  {review.status}; "
            "use versions resume to compile the retained input."
        )


@versions.command("cancel")
@click.argument("review_ids", nargs=-1, required=True)
@click.pass_context
def cancel(ctx, review_ids):
    root, scope = _selection(ctx)
    for identity in review_ids:
        click.echo(cancel_version_review(root, identity, scope=scope).model_dump_json())


@versions.command("resume")
@click.argument("review_ids", nargs=-1, required=True)
@click.pass_context
def resume(ctx, review_ids):
    root, scope = _selection(ctx)
    unsuccessful = False
    for identity in review_ids:
        try:
            result = resume_version_review(root, identity, scope=scope)
        except (OSError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        for line in describe_ingest(result):
            click.echo(line)
        unsuccessful |= result.status not in {"added", "skipped"}
    if unsuccessful:
        ctx.exit(1)
