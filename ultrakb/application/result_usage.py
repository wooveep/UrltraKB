"""Attach committed usage without replacing an already retained business result."""

import logging
from dataclasses import replace

from ultrakb.storage.usage_store import UsageStore

logger = logging.getLogger(__name__)


def merge_result_usage(root, receipts):
    """A statistics read cannot revoke already returned business outcomes."""
    from ultrakb.llm_usage import merge_usage_receipts

    try:
        return merge_usage_receipts(root, receipts), ()
    except Exception:
        logger.warning("Could not confirm combined usage", exc_info=True)
        return None, ("confirm usage",)


def confirm_result_usage(
    result, context, root, identity, *, source_id=None, unit_id=None, execution_id=None
):
    execution_id = execution_id or (
        context.storage_execution.execution_id
        if context is not None and context.storage_execution is not None
        else None
    )
    if execution_id is not None:
        try:
            result = replace(
                result,
                model_usage=UsageStore(root, identity).receipt(
                    execution_id=execution_id,
                    source_id=source_id,
                    unit_id=unit_id,
                ),
            )
        except Exception:
            result = replace(
                result, unfinished=tuple(dict.fromkeys((*result.unfinished, "confirm usage")))
            )
    return result
