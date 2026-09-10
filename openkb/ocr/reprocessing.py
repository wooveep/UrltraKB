"""Explicit, retained decisions to make a new OCR attempt for a physical page."""

from __future__ import annotations

import uuid

from openkb.locks import atomic_write_json
from openkb.mutation import mutation_scope
from openkb.sources import SourceStore, SourceVersion, content_id, read_object, valid_id


def _path(store: SourceStore, source: SourceVersion, profile: dict):
    return store.owned_path(
        store.root / "ocr-reprocessing" / source.id / f"{content_id(profile)}.json"
    )


def decisions(store: SourceStore, source: SourceVersion, profile: dict) -> dict:
    path = _path(store, source, profile)
    records = read_object(path) if path.exists() else {}
    for page, history in records.items():
        if not page.isdigit() or int(page) < 1 or not isinstance(history, list) or not history:
            raise ValueError("Invalid page reprocessing history")
        for row in history:
            if not isinstance(row, dict):
                raise ValueError("Invalid page reprocessing decision")
            valid_id(row.get("attempt"), source=True)
            valid_id(row.get("reviewed_parse"))
            unknown = row.get("acknowledged_unknown_submissions")
            if not isinstance(unknown, list):
                raise ValueError("Invalid acknowledged submission identities")
            for identity in unknown:
                valid_id(identity)
            if "ocr" in row:
                from openkb.ocr.config import OcrSettings

                OcrSettings.model_validate(row["ocr"])
    return records


def page_attempts(store: SourceStore, source: SourceVersion, profile: dict) -> dict[int, str]:
    return {
        int(page): history[-1]["attempt"]
        for page, history in decisions(store, source, profile).items()
    }


def request_page_attempt(
    store: SourceStore,
    source: SourceVersion,
    profile: dict,
    *,
    page: int,
    parse_id: str,
    acknowledge_unknown: bool,
    ocr: dict | None = None,
) -> None:
    unknown = []
    for path in store.owned_path(store.root / "cloud-jobs").glob("*.json"):
        job = read_object(store.owned_path(path))
        value = job.get("input", {})
        if not isinstance(value, dict):
            raise ValueError("Invalid cloud job input")
        if (
            value.get("source") == source.id
            and value.get("page") == page
            and job.get("state") in {"submitting", "submission_unknown"}
        ):
            unknown.append(valid_id(job.get("identity")))
    if unknown and not acknowledge_unknown:
        raise ValueError(
            "A previous cloud submission is unknown; acknowledge it before a new attempt"
        )
    history = decisions(store, source, profile)
    history.setdefault(str(page), []).append(
        {
            "attempt": uuid.uuid4().hex,
            "reviewed_parse": parse_id,
            "acknowledged_unknown_submissions": sorted(unknown),
            **({"ocr": ocr} if ocr is not None else {}),
        }
    )
    path = _path(store, source, profile)
    with mutation_scope(store.kb_dir, [path], operation="request page reprocessing"):
        atomic_write_json(path, history)
