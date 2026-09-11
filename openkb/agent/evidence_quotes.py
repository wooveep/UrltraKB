"""Resolve model quotations to unchanged original character positions."""

from openkb.agent.evidence_retry import ResponseIncomplete

# Word's nonbreaking, figure and narrow nonbreaking spaces can be serialized as
# ordinary spaces by a model. This is a one-character substitution only: never
# normalize digits, punctuation, tabs, line breaks, indentation or whole Unicode.
_TYPESETTING_SPACES = str.maketrans({"\u00a0": " ", "\u2007": " ", "\u202f": " "})


def fact_quote(unit, fact):
    """Validate a fact and return its source spelling and relative [start, end)."""
    details = {
        "unit_id": unit["id"],
        "block_id": unit["reference"]["block_id"],
        "kind": unit["kind"],
        "source_chars": len(unit["text"]),
    }

    def invalid(field, problem):
        raise ResponseIncomplete(
            "fact_evidence_invalid", "facts", field=field, problem=problem, **details
        )

    if not isinstance(fact, dict):
        invalid("fact", "object_required")
    for field in ("topic", "statement", "quote"):
        if not isinstance(fact.get(field), str) or not fact[field].strip():
            invalid(field, "nonempty_text_required")
    text, quote = unit["text"], fact["quote"]
    details["quote_chars"] = len(quote)
    start = text.find(quote)
    if start < 0 and unit["kind"] != "code":
        normalized_text = text.translate(_TYPESETTING_SPACES)
        normalized_quote = quote.translate(_TYPESETTING_SPACES)
        start = normalized_text.find(normalized_quote)
        if start >= 0 and normalized_text.find(normalized_quote, start + 1) >= 0:
            invalid("quote", "ambiguous_typographic_match")
    if start < 0:
        invalid("quote", "not_in_source_unit")
    end = start + len(quote)
    return text[start:end], start, end
