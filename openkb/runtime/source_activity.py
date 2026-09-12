"""Associate live processing items by identity, never by a document's display name."""

from dataclasses import dataclass
from pathlib import Path

from openkb.runtime.records import TERMINAL
from openkb.runtime.requests import ImportFile, ImportUrl, RecompileDocument
from openkb.sources import normalized_origin


@dataclass(frozen=True)
class SourceActivity:
    task_id: str
    state: str
    stage: str
    progress: tuple


def find_source_activity(snapshots, kb_dir, source_id, version_id, origin=None):
    root = str(Path(kb_dir).expanduser().resolve())
    waiting = None
    for view, requests in reversed(snapshots):
        if view.kb_dir != root or view.state in TERMINAL:
            continue
        # The task's stage belongs only to its current item, not later queued documents.
        for index, request in enumerate(requests[len(view.results) :], len(view.results)):
            matches = (
                getattr(request, "source_id", None) == source_id
                and getattr(request, "version_id", None) == version_id
            )
            if isinstance(request, RecompileDocument):
                matches = request.file_hash == source_id
            if isinstance(request, ImportFile) and origin:
                matches = (
                    normalized_origin(Path(request.source), Path(root), request.upload_origin)
                    == origin
                )
            if isinstance(request, ImportUrl) and origin:
                try:
                    matches = normalized_origin(Path(root), Path(root), request.url) == origin
                except ValueError:
                    matches = False  # Invalid unrelated input must not break source observation.
            if matches:
                current = index == len(view.results)
                activity = SourceActivity(
                    view.id,
                    view.state if current else "queued",
                    view.stage if current else "queued",
                    view.progress if current else (),
                )
                if current and view.state in {"running", "stopping"}:
                    return activity
                if waiting is None:
                    waiting = activity
    return waiting
