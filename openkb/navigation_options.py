"""Validation for optional semantic indexing within the shared document budget."""

from openkb.navigation_reading import DEFAULT_WINDOW_TOKENS


def validate_navigation_options(value):
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) - {"enabled", "processing", "window_tokens", "summaries"}
        or type(value.get("enabled", True)) is not bool
        or type(value.get("summaries", True)) is not bool
        or type(value.get("window_tokens", DEFAULT_WINDOW_TOKENS)) is not int
        or value.get("window_tokens", DEFAULT_WINDOW_TOKENS) <= 0
    ):
        raise ValueError("Invalid navigation configuration")
    if value.get("processing") is not None:
        from openkb.processing import RequestLimits

        RequestLimits.from_config(value)
