"""Revalidate authenticated captured responses before any model admission."""

import json

from ultrakb.agent.page_response import PageResponseError
from ultrakb.agent.page_updates import UnchangedPage
from ultrakb.llm_execution import check_model_stop


def reuse_saved_response(
    checkpoints,
    key,
    signature,
    *,
    protocol,
    stage,
    validate,
    metadata,
    sections=(),
    excerpts=None,
    decode=None,
):
    check_model_stop()
    reader = getattr(checkpoints, "retained_response", None)
    if reader is None:
        return None
    captured = reader(key, signature, protocol=protocol, sections=sections, stage=stage)
    if captured is None:
        return None
    raw, origin = captured
    try:
        delivered = validate(raw)
        if decode is not None:
            result = decode(raw, delivered)
        elif isinstance(delivered, UnchangedPage):
            result = {
                "status": "unchanged",
                "base_sha256": delivered.base_sha256,
                "reason": delivered.reason,
            }
        else:
            result = json.loads(delivered)
    except PageResponseError as error:
        if error.code == "page_update_stale":
            raise
        if origin.get("awaiting_validation"):
            checkpoints.record_failure(key, signature, error.code)
        return None
    if excerpts is not None and excerpts.resolutions:
        checkpoints.record_delivery_resolution(
            key, signature, raw, str(delivered), excerpts.resolutions
        )
    checkpoints.save(key, signature, result, reused_response=origin, metadata=metadata)
    return result
