"""Request-local compatibility policy takes precedence over frozen role defaults."""

import pytest

from ultrakb.config import LlmCredentialBundle
from ultrakb.llm_execution import ModelRequest, RoleBindings
from ultrakb.llm_request_options import generation_kwargs


@pytest.mark.unit
def test_explicit_request_drop_policy_survives_nonempty_role_defaults():
    bindings = RoleBindings(
        "openai/gpt-5",
        LlmCredentialBundle(api_key="offline-parameter-key"),
        model_table={},
        generation={"document_index": {"max_tokens": 64}},
    )
    request = ModelRequest(
        [{"role": "user", "content": "Parameter contract only."}],
        operation="document_index",
        stage="structure",
        generation_options={"temperature": 0, "drop_params": True},
    )
    captured = bindings.generation_options("document_index")
    requested = request.payload()["options"]

    result = generation_kwargs(bindings.model("document_index"), None, captured, requested)

    assert result == {"max_tokens": 64, "temperature": 0, "drop_params": True}
    assert bindings.generation_options("document_index") == {"max_tokens": 64}
    assert request.payload()["options"] == {"temperature": 0, "drop_params": True}
