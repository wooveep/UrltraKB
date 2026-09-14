"""Read retained cloud OCR receipts without polling or exposing credentials."""

from typing import Any

from openkb.sources import SourceStore, read_object, valid_id


def cloud_jobs(store: SourceStore, source_id: str | None = None) -> list[dict[str, Any]]:
    """Expose durable requests, including failed downloads and uncertain POSTs."""
    result = []
    for path in sorted(store.owned_path(store.root / "cloud-jobs").glob("*.json")):
        job = read_object(store.owned_path(path))
        value = job.get("input")
        if not isinstance(value, dict):
            raise ValueError("Invalid cloud job input")
        version = store.version(valid_id(value.get("source")))
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
                **counts,
            }
        )
    return result


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
