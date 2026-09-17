"""Find pending OCR work relevant to a cached source and its retained attachments."""

from openkb.docx_containers import MAX_EMBEDDED_DEPTH
from openkb.ocr.reprocessing import effective_attempts
from openkb.processing import processing_checkpoint
from openkb.sources import content_id, read_object


def _family(store, source, parsed, settings, retries, overrides):
    from openkb.evidence import ParseStore

    family = {source.id: (retries, overrides)}
    current = {version.origin: version for version in store.list_sources()}
    pending = [(source, parsed, 0)]

    def include(parent, attachment, depth):
        child_depth = depth + 1
        if child_depth > MAX_EMBEDDED_DEPTH:
            return None
        origin = f"attachment:{parent.source_id}/{content_id(attachment['part'])}"
        child = current.get(origin)
        if child is None or child.blob != attachment["blob"]:
            return None  # A replacement cannot supply work to the retained attachment.
        if child.id not in family:
            family[child.id] = effective_attempts(store, child, settings)
            selected = ParseStore(store.kb_dir).selected(child)
            if selected is not None:
                pending.append((child, selected, child_depth))
        return child

    while pending:
        parent, interpretation, depth = pending.pop()
        processing_checkpoint("parsing")
        for block in interpretation.blocks:
            for attachment in block.location.get("attachment_files", []):
                include(parent, attachment, depth)
            # Historical parses embedded child locations directly in the parent.
            location, ancestor, ancestor_depth = block.location, parent, depth
            while attachment := location.get("attachment"):
                ancestor = include(ancestor, attachment, ancestor_depth)
                if ancestor is None:
                    break
                ancestor_depth += 1
                location = attachment["position"]
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
