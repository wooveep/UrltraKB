"""Estimate the first full-document request without changing content or later budgets."""

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from ultrakb.processing_policy import ModelCapacity, ProcessingDecision


@lru_cache(maxsize=1)
def _model_catalog() -> tuple[dict, str]:
    """Use the installed pinned catalog, never a fetched or runtime alias registry."""
    raw = files("litellm").joinpath("model_prices_and_context_window_backup.json").read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def capacity_policy(
    config: dict,
    *,
    custom_endpoint: bool,
    origin: str | None = None,
    tokenizer_snapshot=None,
    base_url: str | None = None,
) -> dict:
    model = config["model"]
    catalog, fingerprint = _model_catalog()
    explicit = config.get("model_capacity")
    configured = explicit is not None
    if explicit is None and (not custom_endpoint or base_url):
        from ultrakb.model_capabilities import connection_capacity

        explicit = connection_capacity(model, base_url)
    settings = ModelCapacity.model_validate(explicit) if explicit is not None else ModelCapacity()
    byte_guard = settings.max_input_bytes
    measurement_model = settings.tokenizer_model or (None if custom_endpoint else model)
    # Exact keys only: do not remove provider prefixes, infer aliases, or let
    # token_counter's fallback encoding imply that a private model is known.
    card = catalog.get(measurement_model, {})
    known_measurement = card.get("litellm_provider") == "openai" and card.get("mode") == "chat"
    upper_bound = settings.measurement == "utf8_token_upper_bound"
    known_measurement = known_measurement or upper_bound
    from ultrakb.model_tokenizer import tokenizer_asset

    asset = tokenizer_snapshot or tokenizer_asset(config)
    if asset:
        known_measurement = True
    if known_measurement and not asset and not upper_bound:
        import tiktoken

        try:
            tiktoken.encoding_name_for_model(measurement_model)
        except KeyError:
            known_measurement = False
    source = (
        "explicit"
        if configured
        else ("service_profile" if explicit is not None else "bundled_model_catalog")
    )
    if configured and origin:
        source += ":" + origin
    limit = settings.max_input_tokens
    reason = None
    if limit is None and settings.context_window_tokens is not None:
        if settings.output_reserve_tokens is None:
            reason = "Context window is known but output reserve is unknown"
        else:
            limit = max(0, settings.context_window_tokens - settings.output_reserve_tokens)
    elif explicit is None and not custom_endpoint:
        card = catalog.get(model, {})
        raw_limit = card.get("max_input_tokens")
        if type(raw_limit) is int and raw_limit > 0:
            limit = raw_limit
    if not known_measurement:
        reason = "No reliable tokenizer for this model or custom endpoint"
    elif limit is None and reason is None:
        reason = "No reliable input limit for this model"
    if settings.applies_to_model is not None and settings.applies_to_model != model:
        reason = (
            "Capacity preset belongs to another model; "
            "select its connection example or update capacity"
        )
    if byte_guard is not None:
        limit = byte_guard
        reason = None if settings.applies_to_model in {None, model} else reason
    return {
        "model": model,
        "measurement_model": measurement_model if known_measurement else None,
        "input_limit": limit,
        "output_reserve_tokens": settings.output_reserve_tokens,
        "context_window_tokens": settings.context_window_tokens,
        "source": source,
        "unknown_reason": reason,
        "catalog_fingerprint": fingerprint,
        "tokenizer_asset": asset,
        **({"measurement_unit": "utf8_byte"} if byte_guard is not None else {}),
        **({"measurement_unit": "token_upper_bound"} if upper_bound else {}),
        **(
            {"max_output_tokens": settings.max_output_tokens}
            if settings.max_output_tokens is not None
            else {}
        ),
        **(
            {"max_request_bytes": settings.max_request_bytes}
            if settings.max_request_bytes is not None
            else {}
        ),
        **({"provenance": settings.provenance} if settings.provenance else {}),
        **(
            {"input_ceiling": settings.max_input_tokens}
            if settings.context_window_tokens is not None
            else {}
        ),
    }


def captured_capacity_policy(snapshot, model: str) -> dict:
    """Use the execution's frozen settings and tokenizer identity for a model."""
    values = snapshot.values()
    return capacity_policy(
        {**values["effective"], "model": model},
        custom_endpoint=bool(values["credentials"]["base_url"]),
        tokenizer_snapshot=values.get("tokenizer_asset"),
        base_url=values["credentials"]["base_url"],
    )


def compile_measure(snapshot, model: str):
    """Capture the compile input measure once; unknown capacity remains explicit."""
    from ultrakb.model_tokenizer import TokenMeasure

    policy = captured_capacity_policy(snapshot, model)
    return None if policy["unknown_reason"] else TokenMeasure(policy)


def require_compile_capacity(measure, kwargs: dict) -> None:
    """Measure the assembled SDK input, including repair text and generation options."""
    require_request_capacity(measure, kwargs, operation="compile")


def require_request_capacity(measure, kwargs: dict, *, operation: str) -> None:
    """Enforce input capacity after SDK options have been merged for a role."""
    from ultrakb.model_tokenizer import ContextCapacityError
    from ultrakb.request_budget import input_limit

    limit = input_limit(measure, kwargs)
    payload = request_input(kwargs)
    measured = measure.payload_tokens(payload)
    if measured > limit:
        unit = getattr(measure, "unit", "token")
        boundary = "local UTF-8 byte guardrail" if unit == "utf8_byte" else "token input limit"
        raise ContextCapacityError(
            f"Complete {operation} request exceeds captured model capacity; "
            f"{measured} > {limit} local input units ({boundary}). "
            "Review model_capacity input/tokenizer settings; "
            "original input and completed checkpoints are retained"
        )
    if byte_limit := measure.policy.get("max_request_bytes"):
        size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        if size > byte_limit:
            raise ContextCapacityError(
                f"Complete {operation} request exceeds the explicit application byte guard; "
                f"{size} > {byte_limit} UTF-8 bytes"
            )


def request_input(kwargs: dict) -> dict:
    """Credential-free wire input, shared by admission, diagnostics and transport."""
    transport = {
        "model",
        "api_key",
        "base_url",
        "num_retries",
        "max_retries",
        "timeout",
        "extra_headers",
        "drop_params",
        "allowed_openai_params",
    }
    payload = {k: v for k, v in kwargs.items() if k not in transport}
    # OpenAI-compatible SDKs merge extra_body into the HTTP JSON object. Count
    # those fields once in their wire shape, as the window admission does.
    payload.update(payload.pop("extra_body", {}))
    return payload


def select_execution_mode(
    kb_dir: Path,
    processing: ProcessingDecision,
    source_path: Path,
    doc_name: str,
    *,
    scope=None,
    bundle=None,
) -> ProcessingDecision:
    if processing.length_class == "long":
        return processing
    from ultrakb.agent.compiler import get_agents_md, short_document_messages
    from ultrakb.agent.source_windows import CompileSourceWindows
    from ultrakb.config import resolve_credential_bundle, resolve_effective_config
    from ultrakb.config_state import active_values
    from ultrakb.knowledge_scope import resolve_scope

    config, sources = resolve_effective_config(kb_dir)
    credentials = bundle if bundle is not None else resolve_credential_bundle(kb_dir)
    captured = active_values(kb_dir)
    policy = capacity_policy(
        config,
        custom_endpoint=bool(credentials.base_url),
        origin=sources.get("model_capacity", "default"),
        tokenizer_snapshot=captured.get("tokenizer_asset") if captured else None,
        base_url=credentials.base_url,
    )
    updates = {
        "capacity_model": policy["model"],
        "capacity_source": policy["source"],
        "input_limit": policy["input_limit"],
        "output_reserve_tokens": policy["output_reserve_tokens"],
    }
    reason = policy["unknown_reason"]
    if reason:
        return processing.model_copy(update={**updates, "capacity_reason": reason})
    content = source_path.read_bytes().decode("utf-8")
    original = CompileSourceWindows(source_path.parent.parent, doc_name).current_original_evidence(
        content
    )
    messages = short_document_messages(
        doc_name,
        content,
        get_agents_md(resolve_scope(kb_dir, scope).wiki_dir),
        config.get("language", "en"),
        original_evidence=original,
    )
    try:
        from ultrakb.agent.compile_capacity import CompileCapacity
        from ultrakb.agent.summary_protocol import summary_response_format
        from ultrakb.llm_execution import CompletionExecutor, RoleBindings
        from ultrakb.model_tokenizer import TokenMeasure
        from ultrakb.request_budget import fits_request, input_limit

        response_format = summary_response_format(
            "summary-page-json", model=config["model"], base_url=credentials.base_url
        )
        sizing = CompileCapacity(
            CompletionExecutor(
                RoleBindings(config["model"], credentials, generation=config.get("role_generation"))
            ),
            TokenMeasure(policy),
        )
        payload = sizing.payload(messages, {"response_format": response_format})
        measure = sizing.measure
        if measure is None:
            raise ValueError("Unknown request measure")
        measured = measure.payload_tokens(payload)
        updates["input_limit"] = input_limit(measure, payload)
        insufficient = not fits_request(measure, payload)
    except Exception:
        return processing.model_copy(
            update={
                **updates,
                "capacity_reason": "The first request could not be measured reliably",
            }
        )
    return processing.model_copy(
        update={
            **updates,
            "estimated_input_tokens": measured,
            "execution_mode": "segmented" if insufficient else "full",
            "capacity_status": "insufficient" if insufficient else "sufficient",
            "capacity_reason": "First summary request exceeds the input limit"
            if insufficient
            else ("First summary request fits the input limit; later requests may still exceed it"),
        }
    )
