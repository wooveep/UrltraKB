"""Strict, recorded decoding of frozen text bytes; never replace undecodable input."""

import codecs
import re

from openkb.source_records import EncodingDecision, TextDecoding

XML_ENCODING_POLICY = "xml-signature-declaration-v1"


def _xml_encoding(raw: bytes) -> EncodingDecision | None:
    if raw.startswith(
        (
            codecs.BOM_UTF8,
            codecs.BOM_UTF16_LE,
            codecs.BOM_UTF16_BE,
            codecs.BOM_UTF32_LE,
            codecs.BOM_UTF32_BE,
        )
    ):
        return None
    # XML 1.0 appendix F: opening bytes identify these byte orders before
    # interpreting the declaration. Preserve the actual byte order.
    for signature, name in (
        (b"\x00\x00\x00<", "utf-32-be"),
        (b"<\x00\x00\x00", "utf-32-le"),
        (b"\x00<\x00?", "utf-16-be"),
        (b"<\x00?\x00", "utf-16-le"),
    ):
        if raw.startswith(signature):
            return EncodingDecision(name=name, basis="signature", policy=XML_ENCODING_POLICY)
    if raw.startswith(b"<?xml"):
        end = raw.find(b"?>")
        declared = (
            re.search(rb"""\bencoding\s*=\s*["']([^"']+)["']""", raw[:end]) if end >= 0 else None
        )
        if declared:
            try:
                name = codecs.lookup(declared[1].decode("ascii")).name
            except (LookupError, UnicodeDecodeError) as exc:
                raise ValueError("XML declares an unsupported encoding") from exc
            return EncodingDecision(name=name, basis="declaration", policy=XML_ENCODING_POLICY)
    return None


class TextDecodingError(ValueError):
    def __init__(self, message: str, decision: EncodingDecision):
        super().__init__(message)
        self.decision = decision


def inspect_text_encoding(raw: bytes, *, source_format: str | None = None) -> TextDecoding:
    """Admission retains this before format parsing, including decoding failures."""
    diagnostics: tuple[str, ...]
    try:
        decision = _xml_encoding(raw) if source_format == "xml" else None
        if decision is not None:
            try:
                text = raw.decode(decision.name, errors="strict")
                if "\x00" in text:
                    raise ValueError("XML text contains NUL characters")
            except (ValueError, LookupError) as exc:
                raise TextDecodingError(str(exc), decision) from exc
            encoding, diagnostics = decision, ()
        else:
            text, encoding, diagnostics = decode_text(raw)
        if source_format == "xml":
            declared = re.match(
                r"""\ufeff?<\?xml\s+[^?]*?\bencoding\s*=\s*["']([^"']+)["']""", text
            )
            if declared:
                try:
                    name = codecs.lookup(declared[1]).name
                except LookupError as exc:
                    raise TextDecodingError(
                        "XML encoding declaration is unsupported", encoding
                    ) from exc
                actual = codecs.lookup(encoding.name).name
                compatible = name == actual or (
                    name in {"utf-16", "utf-32"} and actual in {name + "-le", name + "-be"}
                )
                if not compatible:
                    raise TextDecodingError(
                        "XML encoding declaration disagrees with its actual encoding", encoding
                    )
    except TextDecodingError as exc:
        return TextDecoding(encoding=exc.decision, diagnostics=(str(exc),), error=str(exc))
    except ValueError as exc:
        return TextDecoding(diagnostics=(str(exc),), error=str(exc))
    return TextDecoding(encoding=encoding, diagnostics=diagnostics, policy=encoding.policy)


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
