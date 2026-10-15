"""Capacity configuration invariants, independent of credentials and tokenizers."""

import pytest
from pydantic import ValidationError

from ultrakb.processing_policy import ModelCapacity

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "values",
    [
        {"context_window_tokens": 128, "output_reserve_tokens": 128},
        {"context_window_tokens": 128, "output_reserve_tokens": 129},
        {"context_window_tokens": 128, "max_input_tokens": 129},
        {"context_window_tokens": 128, "output_reserve_tokens": 32, "max_input_tokens": 97},
    ],
    ids=["reserve-fills-context", "reserve-exceeds-context", "input-exceeds-context", "overlap"],
)
def test_inconsistent_capacity_cannot_claim_question_readiness(values):
    with pytest.raises(ValidationError, match="context"):
        ModelCapacity.model_validate(values)


@pytest.mark.parametrize(
    "values",
    [
        {"context_window_tokens": 128, "output_reserve_tokens": 32, "max_input_tokens": 96},
        {"context_window_tokens": 128, "output_reserve_tokens": 127, "max_input_tokens": 1},
        {"context_window_tokens": 128, "output_reserve_tokens": 0, "max_input_tokens": 128},
        {"context_window_tokens": 128, "output_reserve_tokens": 32},
        {"max_input_tokens": 96},
        {},
    ],
    ids=["exact-fit", "one-input", "zero-reserve", "derive-input", "explicit-input", "unset"],
)
def test_consistent_and_partial_capacity_keeps_the_declared_values(values):
    parsed = ModelCapacity.model_validate(values).model_dump(exclude_none=True)
    assert {key: parsed[key] for key in values} == values


@pytest.mark.parametrize(
    "key", ["context_window_tokens", "output_reserve_tokens", "max_input_tokens"]
)
@pytest.mark.parametrize("value", [True, False, "128", 1.5])
def test_capacity_limits_require_integers(key, value):
    with pytest.raises(ValidationError):
        ModelCapacity.model_validate({key: value})
