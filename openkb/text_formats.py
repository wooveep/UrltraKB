"""Normalize decoded TXT and CSV into the common frozen Markdown contract."""

from pathlib import Path

from openkb.source_records import ENCODING_POLICY, TextDecoding
from openkb.text_encoding import XML_ENCODING_POLICY, decode_text, inspect_text_encoding
from openkb.text_measurement import measure_markdown
from openkb.text_source import NORMALIZATION_POLICY, FrozenText, TextOrigin


def normalize_text_document(
    prepared, doc_name: str, wiki: Path, *, decoding=None, resource_policy=None
) -> FrozenText:
    extension = prepared.source.suffix.lower()
    if extension in {".md", ".markdown"}:
        from openkb.text_source import freeze_markdown

        return freeze_markdown(prepared, doc_name, wiki)
    if extension in {".html", ".htm"}:
        from openkb.html_source import freeze_html

        return freeze_html(prepared, doc_name, wiki, decoding, resource_policy=resource_policy)
    return freeze_decoded_text(prepared, decoding)


def text_normalization_policy(source_format: str) -> str:
    if source_format in {"md", "markdown"}:
        return NORMALIZATION_POLICY
    if source_format == "csv":
        from openkb.csv_source import CSV_POLICY

        return f"{CSV_POLICY}:{ENCODING_POLICY}"
    if source_format == "xml":
        return f"xml-defusedxml-0.7.1-structure-v1:{XML_ENCODING_POLICY}:{ENCODING_POLICY}"
    if source_format in {"html", "htm"}:
        from openkb.html_source import HTML_POLICY

        return f"{HTML_POLICY}:{ENCODING_POLICY}"
    return f"{source_format}-unicode-preserved-v1:{ENCODING_POLICY}"


def freeze_decoded_text(prepared, decoding: TextDecoding | None = None) -> FrozenText:
    raw = prepared.path.read_bytes()
    if decoding is None:
        decoding = inspect_text_encoding(raw, source_format=prepared.path.suffix[1:].lower())
    original, encoding, diagnostics = decode_text(raw, decoding)
    offset = int(original.startswith("\ufeff"))
    text = original[offset:]
    origins = (
        [TextOrigin(normalized_span=(0, 0), original_span=(0, 1), kind="bom")] if offset else []
    )
    origins.append(
        TextOrigin(normalized_span=(0, len(text)), original_span=(offset, len(original)))
    )
    if prepared.path.suffix.lower() == ".csv":
        from openkb.csv_source import normalize_csv

        text, mapped = normalize_csv(original, offset)
        origins = list(mapped)
    elif prepared.path.suffix.lower() == ".xml":
        import re

        from defusedxml.ElementTree import fromstring

        # Validation only: retain declarations, comments, CDATA, attributes and
        # spacing verbatim rather than infer a business schema or reserialize.
        fromstring(text, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
        prefix, suffix = fence + "xml\n", "\n" + fence + "\n"
        origins[-1:] = [
            TextOrigin(
                normalized_span=(0, len(prefix)), original_span=(offset, offset), kind="generated"
            ),
            TextOrigin(
                normalized_span=(len(prefix), len(prefix) + len(text)),
                original_span=(offset, len(original)),
            ),
            TextOrigin(
                normalized_span=(len(prefix) + len(text), len(prefix) + len(text) + len(suffix)),
                original_span=(len(original), len(original)),
                kind="generated",
            ),
        ]
        text = prefix + text + suffix
    return FrozenText(
        text=text,
        original_characters=len(original),
        original_digest=prepared.digest,
        origins=tuple(origins),
        assets={},
        tokens=measure_markdown(text),
        normalization_policy=text_normalization_policy(prepared.path.suffix[1:].lower()),
        encoding=encoding,
        diagnostics=diagnostics,
    )
