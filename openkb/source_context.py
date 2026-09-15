"""Typed source excerpts and reader metadata beside the legacy display context."""

import copy
import json

_TEXT_CONTEXT_INSTRUCTIONS = """When context_data is present, source_excerpts contain parsed source
wording with original row/cell positions. An optional source_kind identifies footnotes,
endnotes, editorial comments or image alt text; retain that role, and never infer image
contents from alt text. Structure describes layout; reader_status is
parser metadata, never an author statement or a requested knowledge fact. A first_row
relation identifies position; position alone does not establish a header role. An
unconfirmed native/header annotation does not refute semantic column labels established
by their actual wording and unambiguous cell relationships. Calling those labels a table
header does not itself claim a native header flag. Preserve actual original wording
even when it resembles a reader status. Generated Markdown headings and table headers
are presentation choices, not by themselves claims that the original used a heading
style or declared a header row. Their labels, values and resulting row/column relations
still need source support. Explicit claims about original styles or native roles need
that exact structural evidence; do not add reader-status commentary to justify formatting.
With context_format=structured_json, the context
string is a paginated JSON serialization of these same fields: join successive context
windows by context_start and follow next until context_complete before interpreting it.
With context_format=legacy_display (or absent), context retains the legacy mixed display;
do not guess that its parser annotations are source quotations."""

_IMAGE_CONTEXT_INSTRUCTIONS = """ Optional image_relations record original asset bytes, full
normalized display frames (orientation/alpha/encoding may change), and assets returned
by OCR for each frame. source_alt is author-supplied alternative text for the original;
it describes that image, not every OCR output. OCR image labels such as 'Original figure'
are reader-generated display labels, not an author's selection of the original image.
Image provenance can establish whole-original or whole-frame extent without interpreting
visual contents; it does not establish the extent or contents of other OCR outputs.
Use these relationships to select an original/display frame when asked for its figure,
without narrating processing metadata unless it is relevant to the user's question."""

CONTEXT_INSTRUCTIONS = _TEXT_CONTEXT_INSTRUCTIONS + _IMAGE_CONTEXT_INSTRUCTIONS


def context_instructions(value):
    """Keep image-derivation guidance with image evidence, not every plain table cell."""

    def images(item):
        if isinstance(item, dict):
            context = item.get("context_data")
            if isinstance(context, dict) and context.get("image_relations"):
                return True
            return any(images(child) for child in item.values())
        if isinstance(item, (list, tuple)):
            return any(images(child) for child in item)
        return False

    return CONTEXT_INSTRUCTIONS if images(value) else _TEXT_CONTEXT_INSTRUCTIONS


def has_structured_context(value):
    """Check projected evidence before adding provenance or interning contexts."""
    if isinstance(value, dict):
        details = value.get("context_data")
        if isinstance(details, dict) and isinstance(details.get("source_excerpts"), list):
            return True
        return any(has_structured_context(item) for item in value.values())
    if isinstance(value, list):
        return any(has_structured_context(item) for item in value)
    return False


def validate_context_data(value):
    if value is None:
        return
    invalid = ValueError("Invalid structured source context")
    required = {"source_excerpts", "structure", "reader_status"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - {"image_relations"}
    ):
        raise invalid
    if "image_relations" in value:
        from openkb.image_provenance import validate_image_relations

        validate_image_relations(value["image_relations"])
    excerpts, structure, status = (
        value["source_excerpts"],
        value["structure"],
        value["reader_status"],
    )
    if (
        not isinstance(excerpts, list)
        or not isinstance(structure, dict)
        or not isinstance(status, dict)
    ):
        raise invalid
    for row in excerpts:
        if (
            not isinstance(row, dict)
            or set(row) - {"text", "row", "cell", "relation", "source_kind"}
            or not {"text", "row", "cell", "relation"} <= set(row)
            or not isinstance(row["text"], str)
            or not isinstance(row["relation"], str)
            or row["relation"] not in {"first_row", "declared_header"}
            or any(type(row[key]) is not int or row[key] < 1 for key in ("row", "cell"))
        ):
            raise invalid
        if "source_kind" in row and (
            not isinstance(row["source_kind"], str)
            or row["source_kind"] not in {"footnote", "endnote", "editorial_comment", "image_alt"}
        ):
            raise invalid
    if set(structure) - {"table", "colspan", "rowspan", "list_level", "ordered"}:
        raise invalid
    for key, item in structure.items():
        if key == "ordered":
            if type(item) is not bool:
                raise invalid
        elif type(item) is not int or item < (0 if key == "list_level" else 1):
            raise invalid
    if status == {} and "image_relations" in value:
        return
    if (
        set(status) != {"header_role"}
        or not isinstance(status["header_role"], str)
        or status["header_role"] not in {"declared", "unconfirmed"}
    ):
        raise invalid


def context_fields(value):
    """Models consume typed context when available; legacy context is never guessed apart."""
    details = getattr(value, "context_data", None)
    if details is not None:
        return {"context_data": copy.deepcopy(details)}
    return {"context": value.context}


def model_context(value):
    """One pageable context stream; the format identifies its field-level provenance."""
    details = getattr(value, "context_data", None)
    if details is not None:
        return json.dumps(details, ensure_ascii=False), "structured_json"
    return value.context, "legacy_display"


def context_size(value):
    details = getattr(value, "context_data", None)
    return len(value.context) + (len(json.dumps(details, ensure_ascii=False)) if details else 0)


def block_record(value):
    """Keep old block and parse digests byte-compatible when the additive field is absent."""
    from dataclasses import asdict

    record = asdict(value)
    if record.get("context_data") is None:
        record.pop("context_data", None)
    return record


def parse_record(value):
    from dataclasses import asdict

    return {**asdict(value), "blocks": [block_record(block) for block in value.blocks]}
