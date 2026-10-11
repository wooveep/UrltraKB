"""Request-local original excerpts: the model selects, the program transcribes.

These anchors preserve supplied text, not entailment or completeness. They do not
turn a partial reader window into a complete procedure or authorize new claims.
"""

import hashlib
import json
import re

from openkb.agent.page_response import PageResponseError, parse_page, parse_summary
from openkb.content_blocks import markdown_layout

PROTOCOL = "original-excerpts-v2"
_MARKER = re.compile(r"^\[\[original:(x\d+\.\d+)\]\][ \t]*$", re.MULTILINE)
_PREFIX = "[[original:"


class SourceExcerpts:
    def __init__(self, sources: list[str]):
        from openkb.llm_execution import check_model_stop

        self.blocks: dict[str, str] = {}
        self.catalog: list[dict] = []
        self.sources: list[dict] = []
        self.literal_bodies: set[str] = set()
        for number, text in enumerate(sources, 1):
            check_model_stop()
            tokens, offsets = markdown_layout(text)
            check_model_stop()
            protected: list[tuple[int, int]] = []
            for index, token in enumerate(tokens):
                if index % 128 == 0:
                    check_model_stop()
                if token.map and token.type in {"fence", "table_open"}:
                    left, right = offsets[token.map[0]], offsets[token.map[1]]
                    if protected and left < protected[-1][1]:
                        protected[-1] = (protected[-1][0], max(right, protected[-1][1]))
                    else:
                        protected.append((left, right))
            for token in tokens:
                # Indentation in extracted native text is not a code delimiter.
                if token.type == "fence":
                    self.literal_bodies.add(token.content.rstrip("\n"))
            # Blank lines are separators, not claims about native PDF headings.
            cuts = [0]
            cursor = 0
            for index, match in enumerate(re.finditer(r"\r?\n[ \t]*\r?\n", text)):
                if index % 128 == 0:
                    check_model_stop()
                end = match.end()
                while cursor < len(protected) and protected[cursor][1] <= end:
                    cursor += 1
                if cursor == len(protected) or end <= protected[cursor][0]:
                    cuts.append(end)
            cuts.append(len(text))
            digest = hashlib.sha256(text.encode()).hexdigest()
            self.sources.append({"source": number, "sha256": digest})
            seen: set[str] = set()
            for ordinal, (start, end) in enumerate(zip(cuts, cuts[1:]), 1):
                if ordinal % 128 == 0:
                    check_model_stop()
                body = text[start:end]
                if not body.strip() or body in seen:
                    continue
                seen.add(body)
                identity = f"x{number}.{ordinal}"
                self.blocks[identity] = body
                lines = body.strip("\r\n").splitlines()
                self.catalog.append(
                    {
                        "id": identity,
                        "source": number,
                        "start": start,
                        "end": end,
                        "opening": lines[0][:120],
                        **({"closing": lines[-1][:120]} if len(lines) > 1 else {}),
                    }
                )
                self.literal_bodies.add(body.replace("\r\n", "\n").strip("\n"))

    def message(self) -> dict:
        return {
            "role": "user",
            "content": "Original excerpt catalog:\n"
            + json.dumps(
                {
                    "protocol": PROTOCOL,
                    "sources": self.sources,
                    "interpretation": (
                        "Sources follow supplied-original order; offsets are Unicode positions. "
                        "Previews locate text, not evidence. Exact repeats within one source share "
                        "its first anchor; all occurrences remain in the original. Anchors prove "
                        "transcription, not an occurrence's applicability, scope or completeness. "
                        "Put a supplied [[original:ID]] alone in a content paragraph outside "
                        "code/HTML; the program copies the whole block. Other fences must be "
                        "closed standalone blocks reproducing a complete supplied block or "
                        "original fenced example, including prefixes, comments and values. "
                        "Carry the selected occurrence's adjacent conditions and operation stage; "
                        "window-edge blocks may be partial. Do not reconstruct commands or table "
                        "alignment from extraction line breaks. Explain lost alignment locally. "
                        "Metadata stays ordinary prose."
                    ),
                    "blocks": self.catalog,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

    def messages(self, messages: list[dict]) -> list[dict]:
        message = self.message()
        return messages if message in messages else [*messages, message]

    def render(self, content: str) -> str:
        tokens, offsets = markdown_layout(content)
        paragraphs = [
            (offsets[token.map[0]], offsets[token.map[1]])
            for index, token in enumerate(tokens)
            if token.type == "inline"
            and token.map
            and token.level == 1
            and index > 0
            and tokens[index - 1].type == "paragraph_open"
            and all(
                _PREFIX not in child.content
                for child in token.children or []
                if child.type in {"html_inline", "code_inline"}
            )
        ]
        for token in tokens:
            if token.type == "fence" and token.map:
                closing = content[offsets[token.map[1] - 1] : offsets[token.map[1]]].rstrip("\r\n")
                if token.map[1] - 1 <= token.map[0] or not re.fullmatch(
                    r" {0,3}"
                    + re.escape(token.markup[0])
                    + "{"
                    + str(len(token.markup))
                    + r",}[ \t]*",
                    closing,
                ):
                    raise PageResponseError(
                        "page_literal_conflict", "A literal excerpt needs its closing fence"
                    )
            if token.type in {"fence", "code_block"} and (
                _PREFIX in token.content or token.content.rstrip("\n") not in self.literal_bodies
            ):
                raise PageResponseError(
                    "page_literal_conflict",
                    "A code excerpt changes or truncates the supplied syntax. "
                    "Select the complete original block using its catalog ID.",
                )
        markers = list(_MARKER.finditer(content))
        if _PREFIX in _MARKER.sub("", content):
            raise PageResponseError(
                "page_excerpt_invalid", "Excerpt references need their own line"
            )
        for match in markers:
            if match.group(1) not in self.blocks:
                raise PageResponseError("page_excerpt_invalid", "Unknown original excerpt ID")
            if not any(start <= match.start() and match.end() <= end for start, end in paragraphs):
                raise PageResponseError(
                    "page_excerpt_invalid", "Excerpt references must be visible prose paragraphs"
                )

        def expand(match):
            body = self.blocks[match.group(1)].strip("\r\n")
            fence = "`" * max(3, 1 + max((len(s) for s in re.findall(r"`+", body)), default=0))
            # Edits may be inserted next to unedited bytes without a separator.
            return f"\n\n{fence}text\n{body}\n{fence}\n\n"

        # One substitution: inserted native text is never interpreted as a new reference.
        rendered = _MARKER.sub(expand, content)
        if any(token.type in {"fence", "code_block"} for token in tokens):
            # Direct verbatim excerpts need the same assembly boundary as anchors.
            rendered = "\n\n" + rendered.strip("\n") + "\n\n"
        return rendered

    def response(self, raw: str, protocol: str, expected_sections=(), *, update=False) -> str:
        if protocol == "markdown":
            return self.render(raw)
        if update:
            try:
                value = json.loads(raw.strip().removeprefix("```json").removesuffix("```"))
            except ValueError:
                return raw  # The existing edit parser owns malformed edit diagnostics.
            if not isinstance(value, dict):
                return raw
            bodies = value.get("edits", [])
            if not isinstance(bodies, list):
                return raw
        elif protocol == "summary-json":
            value = parse_summary(raw, expected_sections)
            bodies = value["sections"]
        else:
            value = parse_page(raw, strict=protocol == "summary-page-json")
            bodies = [value]
        for field in ("description", "type", "reason"):
            if isinstance(value.get(field), str) and _PREFIX in value[field]:
                raise PageResponseError("page_excerpt_invalid", "Metadata cannot contain excerpts")
        for item in bodies:
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                item["content"] = self.render(item["content"])
        return json.dumps(value, ensure_ascii=False)
