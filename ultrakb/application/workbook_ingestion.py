"""Publish each worksheet through the native unit transaction, then aggregate receipts."""

from dataclasses import replace


def aggregate_units(results):
    if not results:
        raise ValueError("No worksheet outcomes to aggregate")
    good = [result for result in results if result.status in {"added", "skipped"}]
    status = (
        "partial"
        if len(good) != len(results)
        else "skipped"
        if all(result.status == "skipped" for result in results)
        else "added"
    )
    if not good:
        status = "failed" if any(result.status == "failed" for result in results) else "blocked"
    if any(result.status == "stopped" for result in results):
        status = "stopped"
    from pathlib import Path

    from ultrakb.llm_usage import merge_usage_receipts

    receipts = [result.model_usage for result in results if result.model_usage]
    usage = (
        merge_usage_receipts(Path(receipts[0]["ledger"]).parent.parent, receipts)
        if receipts
        else None
    )
    return replace(
        results[0],
        status=status,
        units=tuple(unit for result in results for unit in result.units),
        resources=tuple(dict.fromkeys(path for result in results for path in result.resources)),
        quality=tuple(dict.fromkeys(note for result in results for note in result.quality)),
        unfinished=tuple(dict.fromkeys(stage for result in results for stage in result.unfinished)),
        message="; ".join(result.message for result in results if result.message) or None,
        model_usage=usage,
        publication_id=next(
            (result.publication_id for result in reversed(results) if result.publication_id), None
        ),
        projection_pending=any(result.projection_pending for result in results),
    )
