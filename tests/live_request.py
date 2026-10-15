"""Pure request preparation for the budgeted real DeepSeek test transport."""

import copy
import json

MAX_CONTEXT_BYTES = 64 * 1024
MAX_OUTPUT_TOKENS = 2048
DEFAULT_OUTPUT_TOKENS = 1024


def output_allowance(kwargs, *, profile="production"):
    if profile not in {"production", "bounded"}:
        raise ValueError("Unknown real-model request profile")
    options = {**kwargs, **(kwargs.get("extra_body") or {})}
    limits = [
        options[name]
        for name in ("max_tokens", "max_completion_tokens")
        if options.get(name) is not None
    ]
    if len(limits) > 1:
        raise ValueError("Specify only one output token limit")
    if not limits and profile == "production":
        # DeepSeek Chat Completions defaults, verified 2026-10-15. This is a
        # pre-send cost reservation only; never inject it into the request.
        # https://api-docs.deepseek.com/zh-cn/api/create-chat-completion
        effort = options.get("reasoning_effort")
        thinking = (options.get("thinking") or {}).get("type")
        if effort == "max":
            return 131072
        if effort in {"low", "minimal", "medium", "high", "xhigh"} or thinking == "enabled":
            return 65536
        return 8192 if effort == "none" or thinking == "disabled" else 65536
    limit = limits[0] if limits else DEFAULT_OUTPUT_TOKENS
    if type(limit) is not int or limit <= 0:
        raise ValueError("The output token limit must be a positive integer")
    return min(limit, MAX_OUTPUT_TOKENS) if profile == "bounded" else limit


def provider_request(kwargs, *, profile="production"):
    payload = {
        name: copy.deepcopy(kwargs[name])
        for name in (
            "messages",
            "tools",
            "tool_choice",
            "response_format",
            "temperature",
            "top_p",
            "parallel_tool_calls",
            "reasoning_effort",
            "thinking",
            "stop",
            "seed",
            "presence_penalty",
            "frequency_penalty",
        )
        if name in kwargs
    }
    payload.update(copy.deepcopy(kwargs.get("extra_body") or {}))
    allowance = output_allowance(kwargs, profile=profile)
    payload.pop("max_completion_tokens", None)
    payload.update(model="deepseek-flash")
    options = {**kwargs, **(kwargs.get("extra_body") or {})}
    if profile == "bounded" or any(
        options.get(name) is not None for name in ("max_tokens", "max_completion_tokens")
    ):
        payload["max_tokens"] = allowance
    if profile == "bounded":
        effort = payload.get("reasoning_effort")
        payload.setdefault(
            "thinking", {"type": "disabled" if effort in (None, "none") else "enabled"}
        )
    if kwargs.get("stream"):
        payload.update(stream=True)
        payload["stream_options"] = {
            **copy.deepcopy(kwargs.get("stream_options") or {}),
            "include_usage": True,
        }
    context_bytes = len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if context_bytes > MAX_CONTEXT_BYTES:
        raise ValueError(
            f"Real LLM test request is {context_bytes} bytes; "
            f"reduce its fixture to at most {MAX_CONTEXT_BYTES} UTF-8 bytes"
        )
    return payload, allowance


def sdk_request(kwargs, *, api_key, api_base, profile="production"):
    values = dict(kwargs)
    values.update(
        model="deepseek/deepseek-flash",
        api_key=api_key,
        api_base=api_base,
        num_retries=0,
        max_retries=0,
    )
    values.pop("base_url", None)
    name = (
        "max_completion_tokens" if kwargs.get("max_completion_tokens") is not None else "max_tokens"
    )
    allowance = output_allowance(kwargs, profile=profile)
    if profile == "bounded" or kwargs.get(name) is not None:
        values[name] = allowance
    if name == "max_completion_tokens":
        values.pop("max_tokens", None)
    for field in ("messages", "tools", "response_format"):
        if field in values:
            values[field] = copy.deepcopy(values[field])
    body = copy.deepcopy(values.get("extra_body") or {})
    effort = body.get("reasoning_effort", values.get("reasoning_effort"))
    if values.get("thinking") is not None:
        body.setdefault("thinking", copy.deepcopy(values.pop("thinking")))
    elif profile == "bounded":
        body.setdefault("thinking", {"type": "disabled" if effort in (None, "none") else "enabled"})
    if body:
        values["extra_body"] = body
    if values.get("stream"):
        values["stream_options"] = {
            **copy.deepcopy(values.get("stream_options") or {}),
            "include_usage": True,
        }
    return values


def request_changes(before, after):
    """Record adjusted field names, never credentials or request content."""
    return tuple(
        sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    )
