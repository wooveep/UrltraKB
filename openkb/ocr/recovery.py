"""Find pending OCR work for the explicitly selected source only."""

from openkb.processing import processing_checkpoint
from openkb.sources import content_id, read_object


def has_resumable_jobs(store, source, parsed, settings, retries, overrides):
    """Respect current per-page choices and retry identities without sending requests."""
    family = {source.id: (retries, overrides)}
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
        state = record.get("state")
        if state == "rejected" and record.get("service_code") in {10010, 12002}:
            return True  # These explicit rejections prove that no job was accepted.
        if state in {"planned", "submitted", "raw_downloaded"} and record.get("reason") not in {
            "cloud_job_failed",
            "cloud_job_expired",
            "cloud_job_not_found",
        }:
            return True
    return False
