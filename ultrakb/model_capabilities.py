"""Sourced service capabilities; no network lookup or alias guessing at execution time."""

from urllib.parse import urlsplit


def connection_capacity(model: str, base_url: str | None) -> dict | None:
    host = urlsplit(base_url).hostname if base_url else None
    if model == "deepseek/deepseek-flash" and host in {None, "api.deepseek.com"}:
        # Verified 2026-10-16 against the official pricing and Chat Completions docs.
        # Interpret the published "1M" conservatively as decimal one million.
        # UTF-8 bytes bound text tokens; this estimator is not a byte-size cap.
        return {
            "context_window_tokens": 1_000_000,
            "max_output_tokens": 393_216,
            "output_reserve_tokens": 65_536,
            "measurement": "utf8_token_upper_bound",
            "applies_to_model": model,
            "provenance": (
                "deepseek-official-2026-10-16:"
                "https://api-docs.deepseek.com/quick_start/pricing/;"
                "https://api-docs.deepseek.com/api/create-chat-completion"
            ),
        }
    if model == "gpt-5.4" and host in {None, "api.openai.com"}:
        from ultrakb.execution_capacity import _model_catalog

        catalog, fingerprint = _model_catalog()
        card = catalog.get(model, {})
        context, output = card.get("max_input_tokens"), card.get("max_output_tokens")
        if type(context) is int and type(output) is int:
            return {
                "context_window_tokens": context,
                "max_output_tokens": output,
                "output_reserve_tokens": output,
                "measurement": "utf8_token_upper_bound",
                "applies_to_model": model,
                "provenance": "bundled-litellm:" + fingerprint,
            }
    return None
