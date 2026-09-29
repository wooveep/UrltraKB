hello world — this small handbook explains how to preserve a source document.

## Navigation

- [Retain the original](#retain-the-original)
- [Publication states](#publication-states)

## Retain the original

The following example keeps a receipt separate from its evidence. An interrupted
job can have a failed receipt and a perfectly valid older publication. An import
must retain both facts. Combining characters such as café, non-Latin names such
as 資料, and emoji such as 👩🏽‍💻 belong to the exact input, including its spacing.

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class InputRevision:
    """Name one immutable input, including every resource needed to read it.

    The original name is useful for display but is not an identity. Two people
    can independently import identical files, and each source can later receive
    different version annotations. A byte digest allows storage sharing without
    asserting that those sources have the same meaning or release history.
    """

    source_id: str
    revision_id: str
    original: Path
    digest: str
    assets: tuple[Path, ...]


@dataclass(frozen=True)
class Publication:
    """Separate the requested target from the most recent successful input.

    An unsuccessful attempt cannot replace a published body with an error
    message. The reader continues to expose the successful revision while the
    status panel explains what happened to its pending replacement. Explicit
    historical reads select a revision directly and never fall back to newer
    bytes merely because those bytes are easier to locate.
    """

    unit_id: str
    target_revision_id: str
    successful_revision_id: str | None
    knowledge_revision_id: str | None
    status: str


def verify_input(revision: InputRevision, digest_file: Callable[[Path], str]) -> None:
    """Perform validation before calling a converter or an external model.

    Validation failures are useful diagnostic results. They should identify the
    retained resource that cannot be read, preserve the source's previous usable
    publication, and leave enough information for an explicit retry. Recreating
    missing bytes from an unrelated current file would make an apparently
    successful operation impossible to reproduce or audit later.
    """
    if not revision.original.is_file():
        raise ValueError("The retained original is unavailable")
    if digest_file(revision.original) != revision.digest:
        raise ValueError("The retained original changed")
    for resource in revision.assets:
        if not resource.is_file():
            raise ValueError(f"The retained resource is unavailable: {resource.name}")


def describe_publication(publication: Publication) -> str:
    """Use the actual publication when presenting evidence to a reader."""
    if publication.successful_revision_id is None:
        return "The original was retained; no knowledge has been published yet."
    if publication.successful_revision_id != publication.target_revision_id:
        return "Published evidence is available; a newer input remains unfinished."
    return "The current input has a successful publication."
```

## Publication states

The header may be repeated for display when this natural table crosses a block.
Its repeated copy does not count toward document length or become a second source.

| State | Reader behavior | Next action |
| --- | --- | --- |
| Admitted | Original and discovered resources are retained before model calls. | Validate version metadata and prepare processing units. |
| Pending | The requested input has not started compilation. | Schedule it under the knowledge base's existing write lease. |
| Converting | Normalized text and source coordinates are being frozen. | Retain the complete conversion before indexing. |
| Indexing | Content blocks are passed to the original content-based algorithm. | Validate section anchors and retain the complete tree. |
| Compiling | Concepts and summaries are generated against the retained input. | Check generated artifacts before publication. |
| Completed | The current target has a committed knowledge revision. | Read it directly or explicitly choose an older revision. |
| Failed | A previous successful body remains readable if one exists. | Inspect the recorded failure and explicitly retry the target. |
| Interrupted | The process ended while an attempt was in progress. | Recover transactions before accepting another write. |
| Stopped | A cancellation request was observed at a safe boundary. | Preserve completed units and leave pending work visible. |
| Needs version | No version annotation can be chosen without human input. | Present the concrete candidate metadata for clarification. |
| Proposal | Generated changes overlap with existing human edits. | Compare and explicitly accept the proposed replacement. |
| Withdrawn | The source is excluded from automatic future evidence selections. | Retain its immutable history for explicit inspection. |
| Empty selection | The user intentionally selected no applicable versions. | Preserve that choice when the same original is imported again. |
| Historical | An explicit revision supplies its own frozen source and assets. | Label the evidence with the selected version and revision. |
| Refresh required | Published pages depend on an older target revision. | Rebuild from the recorded dependency set under a checked generation. |
| Corrupt asset | An image no longer matches the retained digest. | Report the damaged input instead of silently fetching a replacement. |
| Unknown legacy | A historical artifact predates the physical source map. | Expose its saved text without inventing source coordinates. |

The complete original remains available even when its table of contents has only
one generated root. A heading near the beginning is not evidence of truncation.
