"""Compiler binding to independently durable main-store checkpoints."""

import hashlib
import json
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def document_checkpoints(kb_dir: Path | None, view_id: str, doc_name: str):
    from ultrakb.application.execution import current_execution
    from ultrakb.mutation import RecoveryRequired
    from ultrakb.storage.compile_binding import bound_checkpoints

    if kb_dir is None or current_execution() is None:
        return None
    checkpoints = bound_checkpoints(kb_dir, view_id, doc_name)
    if checkpoints is None:
        raise RecoveryRequired("Main-store compilation requires its fixed inputs and checkpoints")
    return checkpoints
