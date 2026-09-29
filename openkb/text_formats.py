"""Normalize decoded TXT and CSV into the common frozen Markdown contract."""

from openkb.source_records import ENCODING_POLICY, TextDecoding
from openkb.text_encoding import decode_text
from openkb.text_measurement import measure_markdown
from openkb.text_source import NORMALIZATION_POLICY, FrozenText, TextOrigin


def text_normalization_policy(source_format: str) -> str:
    if source_format in {"md", "markdown"}:
        return NORMALIZATION_POLICY
    if source_format == "csv":
        from openkb.csv_source import CSV_POLICY

        return f"{CSV_POLICY}:{ENCODING_POLICY}"
    return f"{source_format}-unicode-preserved-v1:{ENCODING_POLICY}"


def freeze_decoded_text(prepared, decoding: TextDecoding | None = None) -> FrozenText:
    original, encoding, diagnostics = decode_text(prepared.path.read_bytes(), decoding)
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
