"""Privacy-preserving measurements of the complete, credential-free wire input."""

import copy
import json

from ultrakb.request_budget import input_limit

CATEGORIES = (
    "rules_schema",
    "native_catalog",
    "existing_catalog",
    "overview",
    "originals",
    "images",
    "links",
    "correction",
    "planning_protocol",
)


def _category(message, text):
    if message.get("role") == "system":
        return "rules_schema"
    if message.get("role") == "assistant":
        return "overview"
    lower = text.lower()
    if "repair only" in lower or "previous json plan failed" in lower or "diagnostics:" in lower:
        return "correction"
    if "valid [[wikilink]]" in lower or "link candidates" in lower or "link targets" in lower:
        return "links"
    if "generated navigation" in lower or "planning partition (" in lower:
        return "native_catalog"
    if "candidate identity comparison:" in lower or "existing concept pages:" in lower:
        return "existing_catalog"
    if lower.startswith(("plan candidate knowledge", "resolve the candidate's identity")):
        return "planning_protocol"
    if "image catalog" in lower or "figure catalog" in lower:
        return "images"
    if "original" in lower or "source context" in lower:
        return "originals"
    return "planning_protocol"


def _catalog_spans(text):
    concept = text.find("Existing concept pages:\n")
    entity = text.find("\n\nExisting entity pages")
    end = text.find("\n\nReturn a JSON object", entity)
    if concept < 0 or entity < 0 or end < 0:
        return []
    return [
        (concept + len("Existing concept pages:\n"), entity),
        (text.find("\n", entity + 2) + 1, end),
    ]


def _remove_fields(text, keys):
    """Remove catalog fields from the same JSON content shape without prompt disclosure."""
    header, separator, serialized = text.partition("\n")
    if not separator or not serialized.lstrip().startswith(("{", "[")):
        return text
    try:
        value = json.loads(serialized)
    except ValueError:
        return text
    changed = False

    def remove(item):
        nonlocal changed
        if isinstance(item, dict):
            for key in list(item):
                if key in keys:
                    del item[key]
                    changed = True
                else:
                    remove(item[key])
        elif isinstance(item, list):
            for child in item:
                remove(child)

    remove(value)
    return header + "\n" + json.dumps(value, ensure_ascii=False) if changed else text


def _without_text(message, text, category):
    primary = _category(message, text)
    spans = _catalog_spans(text)
    if spans and category in {"existing_catalog", "planning_protocol"}:
        if category == "planning_protocol":
            return "".join(text[start:end] for start, end in spans)
        for start, end in reversed(spans):
            text = text[:start] + text[end:]
        return text
    if primary == category:
        return ""
    if category == "images":
        return _remove_fields(text, {"figures", "image_catalog", "images", "assets"})
    if category == "links":
        return _remove_fields(text, {"link_catalog", "known_targets", "link_candidates"})
    return text


def _without_content(message, category):
    content = message.get("content", "")
    if isinstance(content, str):
        return _without_text(message, content, category)
    if not isinstance(content, list):
        return content
    parts = []
    for part in content:
        if not isinstance(part, dict):
            parts.append(part)
        elif category == "images" and part.get("type") in {"image", "image_url", "input_image"}:
            continue
        elif isinstance(part.get("text"), str):
            parts.append({**part, "text": _without_text(message, part["text"], category)})
        else:
            parts.append(part)
    return parts


def request_diagnostic(payload, measure) -> dict:
    """Report exact whole-request totals and non-additive removal contributions.

    Token boundaries change when a component is removed. Marginal measurements
    must never be presented as additive accounting or provider usage tokens.
    Request options and serialization are measured with all content emptied.
    """
    serialized = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    total = measure.payload_tokens(payload)
    contributions = {}
    for category in CATEGORIES:
        removed = copy.deepcopy(payload)
        for message in removed.get("messages", []):
            message["content"] = _without_content(message, category)
        remaining_bytes = len(json.dumps(removed, ensure_ascii=False, default=str).encode("utf-8"))
        contributions[category] = {
            "local_units_removed": total - measure.payload_tokens(removed),
            "serialized_bytes_removed": len(serialized) - remaining_bytes,
        }
    empty = copy.deepcopy(payload)
    for message in empty.get("messages", []):
        message["content"] = ""
    return {
        "version": 1,
        "unit": measure.unit,
        "provider_billing_tokens": False,
        "input_units": total,
        "input_limit": input_limit(measure, payload),
        "serialized_bytes": len(serialized),
        "serialized_byte_limit": measure.policy.get("max_request_bytes"),
        "output_reserve": payload.get(
            "max_completion_tokens",
            payload.get("max_tokens", measure.policy.get("output_reserve_tokens")),
        ),
        "contributions": contributions,
        "contribution_method": "counterfactual removal; contributions are not additive",
        "options_and_serialization_units": measure.payload_tokens(empty),
        "options_and_serialization_bytes": len(
            json.dumps(empty, ensure_ascii=False, default=str).encode("utf-8")
        ),
    }
