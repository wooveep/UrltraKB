"""Per-request input/output admission, independent of a task's cumulative allowance."""

import json
import time
from dataclasses import dataclass, field

from ultrakb.model_tokenizer import ContextCapacityError


def input_limit(measure, kwargs: dict) -> int:
    policy = measure.policy
    output = kwargs.get("max_completion_tokens", kwargs.get("max_tokens"))
    if output is None:
        output = policy.get("output_reserve_tokens")
    maximum = policy.get("max_output_tokens")
    if maximum is not None and output is not None and output > maximum:
        raise ContextCapacityError("Requested output exceeds captured service output capacity")
    context = policy.get("context_window_tokens")
    if context is None or measure.unit == "utf8_byte":
        return measure.input_limit
    if output is None:
        raise ContextCapacityError("Model context capacity requires an explicit output reserve")
    ceiling = policy.get("input_ceiling", measure.input_limit)
    limit = min(ceiling if ceiling is not None else context, context - output)
    if limit <= 0:
        raise ContextCapacityError("Output reserve leaves no input capacity")
    return limit


def fits_request(measure, kwargs: dict, *, operation="compile") -> bool:
    from ultrakb.execution_capacity import require_request_capacity

    try:
        require_request_capacity(measure, kwargs, operation=operation)
    except ContextCapacityError:
        return False
    return True


@dataclass(frozen=True)
class PreparedRequest:
    """A single frozen SDK request; secrets stay in memory and out of its repr."""

    _json: str = field(repr=False)

    @classmethod
    def capture(cls, kwargs: dict):
        return cls(json.dumps(kwargs, ensure_ascii=False))

    def kwargs(self, policy=None) -> dict:
        value = json.loads(self._json)
        if policy is not None and policy.deadline is not None:
            policy.check()
            remaining = policy.deadline - time.monotonic()
            timeout = value.get("timeout")
            value["timeout"] = min(timeout, remaining) if timeout is not None else remaining
        return value
