"""Carry the successful dispatch's output cap across parsing without guessing from shared state."""

from contextvars import ContextVar

_OUTPUT: ContextVar[int | None] = ContextVar("successful_request_output_tokens", default=None)


def dispatched_output(options):
    value = options.get("max_completion_tokens", options.get("max_tokens"))
    _OUTPUT.set(value if type(value) is int and value > 0 else None)


class ModelText(str):
    output_tokens: int | None

    def __new__(cls, value, output_tokens):
        result = super().__new__(cls, value)
        result.output_tokens = output_tokens
        return result


def model_text(value):
    return ModelText(value, _OUTPUT.get())


def derived_text(value, original):
    return ModelText(value, getattr(original, "output_tokens", None))
