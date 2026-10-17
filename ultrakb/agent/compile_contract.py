"""Request bytes follow the frozen task; acceptance evolves independently."""

import json
from functools import lru_cache
from pathlib import Path

REQUEST_VERSION = 3
ACCEPTANCE_VERSION = "explicit-delivery-v1"


def request_version(checkpoints=None) -> int:
    if checkpoints is not None:
        return getattr(checkpoints, "request_version", REQUEST_VERSION)
    from ultrakb.application.execution import current_execution

    context = current_execution()
    if context and context.model_task:
        return context.model_task.record.compiler_request_version
    return REQUEST_VERSION


@lru_cache(maxsize=1)
def legacy_prompts() -> dict:
    """Historical requests must reproduce their actual bytes, including old advice."""
    path = Path(__file__).resolve().parent.parent / "prompts/compile_request_v1.json"
    return json.loads(path.read_text("utf-8"))


def require_frozen_request(checkpoints, key, signature):
    if require := getattr(checkpoints, "require_request", None):
        require(key, signature)


def historical_summary_repairs(protocol, sections):
    """Keep historical admission and reader-window boundaries reproducible."""
    return tuple(
        {
            **message,
            "content": message["content"].replace(repr(("__ultrakb_sections__",)), repr(sections)),
        }
        for message in legacy_prompts()["summary_repairs"][protocol]
    )
