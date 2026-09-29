"""Strict, recorded decoding of frozen text bytes; never replace undecodable input."""

import codecs

from openkb.source_records import EncodingDecision, TextDecoding


class TextDecodingError(ValueError):
    def __init__(self, message: str, decision: EncodingDecision):
        super().__init__(message)
        self.decision = decision


def inspect_text_encoding(raw: bytes) -> TextDecoding:
    """Admission retains this before format parsing, including decoding failures."""
    try:
        _, encoding, diagnostics = decode_text(raw)
    except TextDecodingError as exc:
        return TextDecoding(encoding=exc.decision, diagnostics=(str(exc),), error=str(exc))
    except ValueError as exc:
        return TextDecoding(diagnostics=(str(exc),), error=str(exc))
    return TextDecoding(encoding=encoding, diagnostics=diagnostics)


def decode_text(
    raw: bytes, frozen: TextDecoding | None = None
) -> tuple[str, EncodingDecision, tuple[str, ...]]:
    """Keep the BOM in decoded coordinates; normalization accounts for removing it."""
    diagnostics: tuple[str, ...] = ()
    if frozen:
        if frozen.error:
            raise ValueError(frozen.error)
        if frozen.encoding is None:
            raise ValueError("Frozen text decoding is missing its encoding")
        return (
            raw.decode(frozen.encoding.name, errors="strict"),
            frozen.encoding,
            frozen.diagnostics,
        )
    for marker, name in (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if raw.startswith(marker):
            decision = EncodingDecision(name=name, basis="bom")
            try:
                text = raw.decode(name, errors="strict")
            except UnicodeDecodeError as exc:
                raise TextDecodingError(str(exc), decision) from exc
            break
    else:
        try:
            text = raw.decode("utf-8", errors="strict")
            decision = EncodingDecision(name="utf-8", basis="utf8")
        except UnicodeDecodeError:
            from charset_normalizer import from_bytes

            match = from_bytes(raw, threshold=0.1, enable_fallback=False).best()
            if match is None:
                raise ValueError(
                    "Text encoding could not be determined without data loss"
                ) from None
            text = raw.decode(match.encoding, errors="strict")
            if text.encode(match.encoding, errors="strict") != raw:
                raise ValueError("Detected text encoding cannot reproduce the original bytes")
            decision = EncodingDecision(
                name=match.encoding,
                basis="detected",
                chaos=match.chaos,
                coherence=match.coherence,
            )
            diagnostics = (f"Encoding inferred as {match.encoding}; verify against the original.",)
    if "\x00" in text:
        raise TextDecodingError(
            "Text input contains NUL characters; refusing a binary input as text", decision
        )
    return text, decision, diagnostics
