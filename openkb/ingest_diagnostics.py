"""Useful persisted failures without storing provider requests or credentials."""

import re

from pydantic import ValidationError


def failure_reason(error: Exception) -> str:
    category = type(error).__name__
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)[:3]
        )
    if type(error).__module__.split(".")[0] in {"litellm", "openai", "httpx", "anthropic"}:
        # These exceptions often embed the full HTTP request in str(error).
        reasons = {
            "AuthenticationError": "Provider rejected the credentials",
            "PermissionDeniedError": "Provider denied access to the selected model",
            "NotFoundError": "Provider could not find the selected model or endpoint",
            "RateLimitError": "Provider rate or quota limit reached",
            "ContextWindowExceededError": "Input exceeds the model context window",
            "ContentPolicyViolationError": "Provider rejected this content",
            "Timeout": "Provider did not respond before the request deadline",
            "APITimeoutError": "Provider did not respond before the request deadline",
            "APIConnectionError": "Could not connect to the provider",
            "BadRequestError": "Provider rejected the request parameters",
        }
        detail = reasons.get(category, f"Provider request failed ({category})")
        status = getattr(error, "status_code", None)
        return f"{detail}; HTTP {status}" if isinstance(status, int) else detail
    message = str(error).splitlines()[0] if str(error) else category
    message = re.sub(r"https?://\S+", "[endpoint]", message)
    message = re.sub(r"(?i)(bearer\s+|sk-)[\w.\-]+", "[credential]", message)
    message = re.sub(
        r"(?i)(api[_-]?key|authorization|token|password)([\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+",
        r"\1\2[redacted]",
        message,
    )
    return message[:500]
