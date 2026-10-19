"""Refresh optional task usage without losing a confirmed cached result."""

from dataclasses import replace
from pathlib import Path

from ultrakb.application.task_usage import task_usage_receipt


def with_retained_usage(view, results):
    try:
        usage = task_usage_receipt(
            Path(view.kb_dir),
            view.id,
            receipts=[result.model_usage for result in results if result.model_usage],
        )
    except Exception:
        unavailable = "Retained task details are unavailable; showing cached status"
        return replace(view, error="\n".join(filter(None, (view.error, unavailable))))
    return replace(view, model_usage=usage or view.model_usage)
