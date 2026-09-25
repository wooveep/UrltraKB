"""Carry the successful dispatch's output cap across parsing without guessing from shared state."""

from contextvars import ContextVar

_OUTPUT: ContextVar[int | None] = ContextVar("successful_request_output_tokens", default=None)


def dispatched_output(options):
    value = options.get("max_completion_tokens", options.get("max_tokens"))
    _OUTPUT.set(value if type(value) is int and value > 0 else None)


class ModelText(str):
    output_tokens: int | None

    def __new__(
        cls,
        value,
        output_tokens,
        *,
        raw_content=None,
        finish_reason=None,
        representation="decoded",
    ):
        result = super().__new__(cls, value)
        result.output_tokens = output_tokens
        result.raw_content = raw_content
        result.finish_reason = finish_reason
        result.representation = representation
        return result


def model_text(value, *, raw_content=None, finish_reason=None, representation="decoded"):
    return ModelText(
        value,
        _OUTPUT.get(),
        raw_content=raw_content,
        finish_reason=finish_reason,
        representation=representation,
    )


def derived_text(value, original):
    return ModelText(value, getattr(original, "output_tokens", None), representation="derived")
