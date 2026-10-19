"""Attach committed usage without replacing an already retained business result."""

from dataclasses import replace

from ultrakb.storage.usage_store import UsageStore


def confirm_result_usage(result, context, root, identity, *, source_id=None, unit_id=None):
    if context is not None and context.storage_execution is not None:
        try:
            result = replace(
                result,
                model_usage=UsageStore(root, identity).receipt(
                    execution_id=context.storage_execution.execution_id,
                    source_id=source_id,
                    unit_id=unit_id,
                ),
            )
        except Exception:
            result = replace(
                result, unfinished=tuple(dict.fromkeys((*result.unfinished, "confirm usage")))
            )
    return result
