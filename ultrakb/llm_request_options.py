"""Merge frozen role defaults with explicit request generation parameters."""


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
