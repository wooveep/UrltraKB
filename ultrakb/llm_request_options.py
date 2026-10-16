"""Assemble captured SDK inputs and role generation options without sending calls."""

import time


def generation_kwargs(model: str, base_url: str | None, captured: dict, requested: dict) -> dict:
    options = {**captured, **requested}
    if "max_completion_tokens" in requested:
        options.pop("max_tokens", None)
        if model.startswith("deepseek/"):
            options["max_tokens"] = options.pop("max_completion_tokens")
    if "reasoning_effort" in options and base_url and model.startswith("openai/"):
        # An explicit compatible gateway can support parameters absent from the
        # SDK model table. Keep the chosen request value on the HTTP boundary.
        options["allowed_openai_params"] = ["reasoning_effort"]
    if "reasoning_effort" in options and model.startswith("deepseek/"):
        options["extra_body"] = {"reasoning_effort": options.pop("reasoning_effort")}
    if captured:
        options.setdefault("drop_params", False)
    return options


def prepare_request(bindings, policy, role: str, request) -> dict:
    """One assembly path for capacity planners and model transports."""
    from ultrakb.llm_runtime import audit_step_headers
    from ultrakb.summary_delivery import summary_transport_kwargs

    policy.check()
    bundle = bindings.credentials()
    payload = request.payload()
    messages = payload["messages"]
    from ultrakb.llm_images import require_text_input

    require_text_input(messages)
    model = bindings.model(role)
    captured_options = bindings.generation_options(role)
    # Cache segments are provider hints, never application cache hits.
    if "anthropic" in model or "claude" in model:
        for i in payload["cache_segments"]:
            content = messages[i].get("content")
            if isinstance(content, str):
                messages[i]["content"] = [
                    {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                ]
    kwargs = {
        "model": model,
        "messages": messages,
        "api_key": bundle.api_key,
        "base_url": bundle.base_url,
        "num_retries": 0,
        "max_retries": 0,
        **generation_kwargs(model, bundle.base_url, captured_options, payload["options"]),
    }
    if payload["tools"] is not None:
        kwargs["tools"] = payload["tools"]
    headers = audit_step_headers(bundle.extra_headers, request.stage)
    if headers:
        kwargs["extra_headers"] = headers
    timeout = bundle.timeout
    if policy.deadline is not None:
        remaining = policy.deadline - time.monotonic()
        timeout = min(timeout, remaining) if timeout is not None else remaining
    if timeout is not None:
        kwargs["timeout"] = timeout
    return summary_transport_kwargs(kwargs)
