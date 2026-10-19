"""Provider-reported token accounting; unknown details are never estimated."""

from dataclasses import asdict, dataclass
from typing import Any


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


@dataclass(frozen=True)
class TokenUsage:
    input_cached: int | None = None
    input_uncached: int | None = None
    output: int | None = None
    reasoning: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "TokenUsage":
        if not isinstance(value, dict) or any(
            v is not None and _count(v) is None for v in value.values()
        ):
            raise ValueError("Invalid token usage")
        if set(value) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown token usage fields")
        return cls(**value)

    @classmethod
    def from_provider(cls, value: Any) -> "TokenUsage":
        total = _count(_field(value, "input_tokens"))
        cached = _count(_field(_field(value, "input_tokens_details"), "cached_tokens"))
        if total is not None and cached is not None and cached > total:
            cached = None
        return cls(
            cached,
            total - cached if total is not None and cached is not None else None,
            _count(_field(value, "output_tokens")),
            _count(_field(_field(value, "output_tokens_details"), "reasoning_tokens")),
        )

    def plus(self, other: "TokenUsage") -> "TokenUsage":
        # Missing breakdown in any request makes the corresponding total unknown.
        return TokenUsage(
            **{
                key: a + b if a is not None and b is not None else None
                for key, a in self.to_dict().items()
                for b in (getattr(other, key),)
            }
        )


def add_usage(previous: dict | None, current: dict | None) -> dict | None:
    if current is None:
        return previous
    usage = TokenUsage.from_dict(current)
    return (TokenUsage.from_dict(previous).plus(usage) if previous is not None else usage).to_dict()


def execution_usage(fallback: dict | None = None) -> dict | None:
    """Project the shared transport ledger; SDK-only callers keep their reported usage."""
    from ultrakb.llm_usage import active_scope

    scope = active_scope()
    if scope is None or scope.storage is None:
        return fallback
    total = scope.storage.usage.summary(execution_id=scope.execution_id)
    if not total["requests"]:
        return fallback
    return aggregate_token_usage(total)


def aggregate_token_usage(total: dict) -> dict:
    unknown = total["unknown_fields"]
    cached = None if unknown["cached_input"] else total["cached_input"]
    input_total = None if unknown["input_total"] else total["input_total"]
    return TokenUsage(
        cached,
        input_total - cached if input_total is not None and cached is not None else None,
        None if unknown["output_total"] else total["output_total"],
        None if unknown["reasoning_output"] else total["reasoning_output"],
    ).to_dict()
