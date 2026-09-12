"""Validation for optional semantic indexing within the shared document budget."""


def validate_navigation_options(value):
    if value is None:
        return
    if (
        not isinstance(value, dict)
        or set(value) - {"enabled", "processing"}
        or type(value.get("enabled", True)) is not bool
    ):
        raise ValueError("Invalid navigation configuration")
    if value.get("processing") is not None:
        from openkb.processing import RequestLimits

        RequestLimits.from_config(value)
