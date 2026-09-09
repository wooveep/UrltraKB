"""Bounded cleanup of model resources owned by the current compiler loop.

The logging queue adapter follows the pinned LiteLLM implementation. It only
observes that loop's queue; the isolated document worker supervises final exit.
"""

from __future__ import annotations

import asyncio
import logging

from openkb.compilation_report import report_auxiliary_warning
from openkb.processing import cleanup_limit


async def close_model_resources() -> None:
    import litellm
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER as worker

    timeout = cleanup_limit()
    try:
        if worker._bound_loop is asyncio.get_running_loop():
            await asyncio.wait_for(worker.flush(), timeout=timeout)
    except Exception:
        report_auxiliary_warning("auxiliary_log_cleanup_failed")
        logging.getLogger(__name__).debug("Model logging cleanup failed", exc_info=True)
    try:
        await asyncio.wait_for(litellm.close_litellm_async_clients(), timeout=timeout)
    except Exception:
        report_auxiliary_warning("model_client_cleanup_failed")
        logging.getLogger(__name__).debug("Model client cleanup failed", exc_info=True)
