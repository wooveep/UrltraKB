"""Read retained cloud OCR receipts without polling or exposing credentials."""

import re
from typing import Any

from openkb.sources import SourceStore, content_id, read_object, valid_id


def cloud_jobs(store: SourceStore, source_id: str | None = None) -> list[dict[str, Any]]:
    """Expose durable requests, including failed downloads and uncertain POSTs."""
    result = []
    for path in sorted(store.owned_path(store.root / "cloud-jobs").glob("*.json")):
        job = read_object(store.owned_path(path))
        value = job.get("input")
        if not isinstance(value, dict):
            raise ValueError("Invalid cloud job input")
        try:
            version = store.version(valid_id(value.get("source")))
        except (TypeError, KeyError):
            raise ValueError("Invalid cloud job source version") from None
        if source_id is not None and version.source_id != source_id:
            continue
        if any(
            job.get(field) is not None and not isinstance(job[field], str)
            for field in ("state", "remote_state", "reason", "job_id")
        ):
            raise ValueError("Invalid cloud job state")
        counts = {}
        for field in ("requests", "submissions", "download_bytes"):
            count = job.get(field, 0)
            if type(count) is not int or count < 0:
                raise ValueError("Invalid cloud job accounting")
            counts[field] = count
        result.append(
            {
                "version_id": version.id,
                "page": value.get("page"),
                "identity": job.get("identity"),
                "job_id": job.get("job_id"),
                "state": job.get("state"),
                "remote_state": job.get("remote_state"),
                "reason": job.get("reason"),
                "receipt_verified": _receipt_verified(path.stem, job, value),
                **counts,
            }
        )
    return result


def _receipt_verified(identity, job, value):
    """Legacy or incomplete receipts retain their raw fields, never inferred acceptance."""
    if (
        job.get("identity") != identity
        or content_id(value) != identity
        or set(value) != {"source", "page", "slice", "profile"}
        or type(value.get("page")) is not int
        or value["page"] < 1
        or not isinstance(value.get("profile"), dict)
        or value["profile"].get("physical_page") != value["page"]
        or value["profile"].get("slice") != value.get("slice")
    ):
        return False
    state, job_id = job.get("state"), job.get("job_id")
    accepted = {"submitted", "raw_downloaded", "downloaded"}
    if state not in accepted | {"planned", "submitting", "submission_unknown", "rejected"}:
        return False
    if state in accepted:
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", job_id):
            return False
    elif job_id is not None:
        return False
    try:
        valid_id(value.get("slice"))
        if state in {"raw_downloaded", "downloaded"}:
            valid_id(job.get("result_blob"))
        if state == "downloaded":
            valid_id(job.get("parse_id"))
    except ValueError:
        return False
    return True


def source_job_snapshots(store, sources):
    """Capture once under the caller's KB lock, separately from published evidence."""
    snapshots = {
        source.id: {
            "scope": "local_receipts_for_source_version",
            "version": source.id,
            "status": "available",
            "jobs": [],
        }
        for source in sources
    }
    if not snapshots:
        return snapshots
    try:
        jobs = cloud_jobs(store)
    except (OSError, ValueError):
        # Optional operational metadata cannot make intact original evidence
        # unreadable. An unavailable ledger proves neither submission nor absence.
        for snapshot in snapshots.values():
            snapshot["status"] = "unavailable"
        return snapshots
    groups = {}
    for job in jobs:
        version = job["version_id"]
        if version not in snapshots:
            continue
        if not job["receipt_verified"]:
            job = {
                **job,
                "state": "unknown",
                "remote_state": None,
                "reason": "unverified_job_receipt",
                "job_id": None,
            }
        state = job["state"]
        submission = (
            "rejected_before_acceptance"
            if state == "rejected" and not job["job_id"]
            else "accepted"
            if job["job_id"]
            else "not_submitted"
            if state == "planned"
            else "unknown"
        )
        key = version, state, job["remote_state"], job["reason"], submission
        if key not in groups:
            groups[key] = {
                "state": state,
                "remote_state": job["remote_state"],
                "reason": job["reason"],
                "submission": submission,
                "count": 0,
            }
            snapshots[version]["jobs"].append(groups[key])
        groups[key]["count"] += 1
    return snapshots
