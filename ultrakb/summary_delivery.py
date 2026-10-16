"""Documented summary transport capabilities, independent of model price aliases.

DeepSeek Chat Completions only supports JSON mode. Its Responses endpoint supports
JSON Schema; the pinned SDK already bridges that wire format back to chat results.
References: https://api-docs.deepseek.com/zh-cn/api/create-response/
and https://api-docs.deepseek.com/zh-cn/guides/responses_api/
"""

from urllib.parse import urlsplit


def deepseek_summary_schema(model: str, base_url: str | None) -> bool:
    if model != "deepseek/deepseek-flash":
        return False
    if base_url is None:
        return True
    endpoint = urlsplit(base_url)
    return (
        endpoint.scheme == "https"
        and endpoint.netloc == "api.deepseek.com"
        and endpoint.path.rstrip("/") in {"", "/v1"}
        and not endpoint.query
        and not endpoint.fragment
    )


def summary_transport_kwargs(kwargs: dict) -> dict:
    """Adapt only canonical DeepSeek summary schemas at the shared SDK boundary."""
    response_format = kwargs.get("response_format") or {}
    definition = response_format.get("json_schema") or {}
    if (
        response_format.get("type") != "json_schema"
        or definition.get("name") not in {"summary_json", "summary_page_json"}
        or not deepseek_summary_schema(kwargs["model"], kwargs.get("base_url"))
    ):
        return kwargs
    mapped = {
        **kwargs,
        "model": "openai/responses/deepseek-flash",
        # Use the documented Responses root, including when the captured chat
        # endpoint was /v1. Authentication and the public task model stay frozen.
        "base_url": "https://api.deepseek.com",
    }
    extra = dict(mapped.get("extra_body") or {})
    if "reasoning_effort" in extra:
        mapped["reasoning_effort"] = extra.pop("reasoning_effort")
        mapped["allowed_openai_params"] = ["reasoning_effort"]
        if extra:
            mapped["extra_body"] = extra
        else:
            mapped.pop("extra_body", None)
    return mapped


def preserve_responses_status(response, status: str | None) -> None:
    """The pinned SDK's chat bridge otherwise marks partial output as 'stop'."""
    if status == "incomplete":
        for choice in response.choices:
            choice.finish_reason = "length"
    elif status and status != "completed":
        raise ValueError(f"Native Responses reply did not complete: {status}")
