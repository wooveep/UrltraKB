"""Find accepted work still relevant to a cached source and its retained attachments."""

from openkb.ocr.reprocessing import effective_attempts
from openkb.processing import processing_checkpoint
from openkb.sources import content_id, read_object


def _family(store, source, parsed, settings, retries, overrides):
    family = {source.id: (retries, overrides)}
    if not any("attachment" in block.location for block in parsed.blocks):
        return family
    current = {version.origin: version for version in store.list_sources()}
    for block in parsed.blocks:
        parent = source
        location = block.location
        while attachment := location.get("attachment"):
            origin = f"attachment:{parent.source_id}/{content_id(attachment['part'])}"
            child = current.get(origin)
            if child is None or child.blob != attachment["blob"]:
                break  # A replacement attachment cannot supply work to this retained one.
            if child.id not in family:
                family[child.id] = effective_attempts(store, child, settings)
            parent, location = child, attachment["position"]
    return family


def has_resumable_jobs(store, source, parsed, settings, retries, overrides):
    """Respect current per-page choices and retry identities without sending requests."""
    family = _family(store, source, parsed, settings, retries, overrides)
    for path in store.owned_path(store.root / "cloud-jobs").glob("*.json"):
        processing_checkpoint("parsing")
        record = read_object(store.owned_path(path))
        intent = record.get("input", {})
        if not isinstance(intent, dict) or intent.get("source") not in family:
            continue
        attempts, page_settings = family[intent["source"]]
        page = intent.get("page")
        effective = page_settings.get(page, settings)
        if effective.policy == "off" or effective.backend != "cloud" or effective.cloud is None:
            continue
        profile = intent.get("profile", {})
        if (
            not isinstance(profile, dict)
            or profile.get("ocr") != effective.cloud.profile()
            or profile.get("reprocessing") != attempts.get(page)
        ):
            continue
        if record.get("identity") != content_id(intent) or path.stem != record["identity"]:
            raise ValueError("Cloud OCR recovery identity mismatch")
        if record.get("state") in {"submitted", "raw_downloaded"} and record.get("reason") not in {
            "cloud_job_failed",
            "cloud_job_expired",
            "cloud_job_not_found",
        }:
            return True
    return False
