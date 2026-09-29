"""Estimate the first full-document request without changing content or later budgets."""

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

from openkb.processing_policy import ModelCapacity, ProcessingDecision


@lru_cache(maxsize=1)
def _model_catalog() -> tuple[dict, str]:
    """Use the installed pinned catalog, never a fetched or runtime alias registry."""
    raw = files("litellm").joinpath("model_prices_and_context_window_backup.json").read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def capacity_policy(config: dict, *, custom_endpoint: bool, origin: str | None = None) -> dict:
    model = config["model"]
    catalog, fingerprint = _model_catalog()
    explicit = config.get("model_capacity")
    settings = ModelCapacity.model_validate(explicit) if explicit is not None else ModelCapacity()
    measurement_model = settings.tokenizer_model or (None if custom_endpoint else model)
    # Exact keys only: do not remove provider prefixes, infer aliases, or let
    # token_counter's fallback encoding imply that a private model is known.
    card = catalog.get(measurement_model, {})
    known_measurement = card.get("litellm_provider") == "openai" and card.get("mode") == "chat"
    if known_measurement:
        import tiktoken

        try:
            tiktoken.encoding_name_for_model(measurement_model)
        except KeyError:
            known_measurement = False
    source = "explicit" if explicit is not None else "bundled_model_catalog"
    if explicit is not None and origin:
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
    return {
        "model": model,
        "measurement_model": measurement_model if known_measurement else None,
        "input_limit": limit,
        "output_reserve_tokens": settings.output_reserve_tokens,
        "source": source,
        "unknown_reason": reason,
        "catalog_fingerprint": fingerprint,
    }


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
    import litellm

    from openkb.agent.compiler import get_agents_md, short_document_messages
    from openkb.config import resolve_credential_bundle, resolve_effective_config
    from openkb.knowledge_scope import resolve_scope

    config, sources = resolve_effective_config(kb_dir)
    credentials = bundle if bundle is not None else resolve_credential_bundle(kb_dir)
    policy = capacity_policy(
        config,
        custom_endpoint=bool(credentials.base_url),
        origin=sources.get("model_capacity", "default"),
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
    messages = short_document_messages(
        doc_name,
        source_path.read_bytes().decode("utf-8"),
        get_agents_md(resolve_scope(kb_dir, scope).wiki_dir),
        config.get("language", "en"),
    )
    try:
        import tiktoken

        encoding = tiktoken.encoding_for_model(policy["measurement_model"])
        # Use the public custom-tokenizer seam to prevent LiteLLM from guessing
        # a fallback model or downloading a provider tokenizer.
        tokenizer = SimpleNamespace(
            encode=lambda text: SimpleNamespace(ids=encoding.encode(text, disallowed_special=()))
        )
        measured = litellm.token_counter(
            model=policy["measurement_model"],
            messages=messages,
            custom_tokenizer={"type": "huggingface_tokenizer", "tokenizer": tokenizer},
        )
        if litellm.disable_token_counter or type(measured) is not int or measured <= 0:
            raise ValueError("Invalid token measurement")
    except Exception:
        return processing.model_copy(
            update={
                **updates,
                "capacity_reason": "The first request could not be measured reliably",
            }
        )
    insufficient = measured > policy["input_limit"]
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
