"""Locate a paid OCR job for the identical input slice and recognition profile."""

from openkb.processing import processing_checkpoint
from openkb.sources import content_id, read_object


def matching_job(store, intent):
    expected = {key: value for key, value in intent.items() if key != "source"}
    for path in store.owned_path(store.root / "cloud-jobs").glob("*.json"):
        processing_checkpoint("ocr")
        try:
            record = read_object(store.owned_path(path))
            original = record.get("input")
            if not isinstance(original, dict):
                continue
            if {key: value for key, value in original.items() if key != "source"} != expected:
                continue
            if record.get("identity") != path.stem or content_id(original) != path.stem:
                continue
            store.version(original["source"])
            # Known/uncertain jobs are shared as well: a second document must
            # never submit the same image while its first submission is unknown.
            return path, original
        except (ValueError, KeyError, FileNotFoundError):
            continue
    return None
