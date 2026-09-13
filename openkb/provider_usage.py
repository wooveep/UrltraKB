"""Keep missing wire usage distinct from SDK-generated zero counters."""

import json


class WireUsage:
    def __init__(self, options):
        self.complete = None
        self.previous = options.get("logger_fn")
        options["logger_fn"] = self.observe

    def observe(self, details):
        if details.get("log_event_type") == "post_api_call":
            raw = details.get("original_response")
            try:
                value = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                value = None
            # Only interpret the OpenAI chat wire contract here. Other provider
            # formats keep their adapter's usage handling. Never retain raw text.
            if isinstance(value, dict) and isinstance(value.get("choices"), list):
                usage = value.get("usage")
                self.complete = isinstance(usage, dict) and all(
                    type(usage.get(key)) is int and usage[key] >= 0
                    for key in ("prompt_tokens", "completion_tokens")
                )
        if callable(self.previous):
            self.previous(details)

    def restore(self, response):
        if self.complete is False:
            response.usage = None
        return response
