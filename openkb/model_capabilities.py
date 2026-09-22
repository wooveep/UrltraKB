"""Read local SDK model-capability declarations without making provider requests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ModelCapabilities:
    """One selected model's independently declared input/output limits."""

    context_tokens: int
    input_tokens: int | None
    max_output_tokens: int
    shared_context: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def selected_model_capabilities(model: Any, endpoint: Any = None) -> ModelCapabilities | None:
    """Return a locally known capability contract, never probing a provider."""

    if not isinstance(model, str) or not model:
        return None
    try:
        import litellm

        info = litellm.get_model_info(
            model=model, **({"api_base": endpoint} if isinstance(endpoint, str) else {})
        )
    except Exception:
        return None

    def positive(name: str) -> int | None:
        value = info.get(name) if isinstance(info, dict) else getattr(info, name, None)
        return value if type(value) is int and value > 0 else None

    input_tokens = positive("max_input_tokens")
    output_tokens = positive("max_output_tokens")
    total_tokens = positive("max_tokens")
    if input_tokens and output_tokens:
        return ModelCapabilities(input_tokens, input_tokens, output_tokens, False)
    if total_tokens and output_tokens and output_tokens < total_tokens:
        return ModelCapabilities(total_tokens, None, output_tokens, True)
    return None


def capability_from_dict(value: Any) -> ModelCapabilities | None:
    """Decode a snapshot capability contract without trusting arbitrary settings."""

    if not isinstance(value, dict):
        return None
    fields = {"context_tokens", "input_tokens", "max_output_tokens", "shared_context"}
    if set(value) != fields:
        return None
    context, input_tokens, output, shared = (
        value["context_tokens"],
        value["input_tokens"],
        value["max_output_tokens"],
        value["shared_context"],
    )
    if (
        type(context) is not int
        or context <= 0
        or (input_tokens is not None and (type(input_tokens) is not int or input_tokens <= 0))
        or type(output) is not int
        or output <= 0
        or type(shared) is not bool
        or (shared and input_tokens is not None)
        or (not shared and input_tokens is None)
    ):
        return None
    return ModelCapabilities(context, input_tokens, output, shared)
