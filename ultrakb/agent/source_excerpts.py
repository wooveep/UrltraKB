"""Request-local original excerpts: the model selects, the program transcribes.

These anchors preserve supplied text, not entailment or completeness. They do not
turn a partial reader window into a complete procedure or authorize new claims.
"""

import hashlib
import json
import re
from collections.abc import Iterable
from functools import cached_property

from ultrakb.agent.compile_contract import legacy_prompts, request_version
from ultrakb.agent.excerpt_matching import ExcerptIndex, problem
from ultrakb.agent.page_response import PageResponseError, parse_page, parse_summary
from ultrakb.content_blocks import markdown_layout

PROTOCOL = "original-excerpts-v3"
_MARKER = re.compile(r"^\[\[original:(x\d+\.\d+)\]\][ \t]*$", re.MULTILINE)
_PREFIX = "[[original:"


class SourceExcerpts:
    def __init__(self, sources: Iterable[str | dict], *, request_format: int | None = None):
        from ultrakb.llm_execution import check_model_stop

        self.request_version = request_version() if request_format is None else request_format
        sources = list(sources)
        self._input_sources = sources
        self.resolutions: list[dict] = []
        self.blocks: dict[str, str] = {}
        self.catalog: list[dict] = []
        self.sources: list[dict] = []
        for number, source in enumerate(sources, 1):
            check_model_stop()
            text = source if isinstance(source, str) else source["content"]
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

    @cached_property
    def index(self):
        return ExcerptIndex(self._input_sources, self.catalog)

    @cached_property
    def references(self):
        return self.index.references()

    def message(self, *, compact: bool = False) -> dict:
        return {
            "role": "user",
            "content": "Original excerpt catalog:\n"
            + json.dumps(
                {
                    "protocol": PROTOCOL,
                    "sources": self.sources,
                    "interpretation": (
                        legacy_prompts()["excerpt_catalog"]
                        if self.request_version == 1
                        else "Sources follow supplied-original order; offsets are Unicode "
                        "positions. "
                        "Previews locate text, not evidence. Put a supplied [[original:ID]] alone "
                        "in an unindented top-level content paragraph outside code, lists, HTML "
                        "and blockquotes; the program copies its original block. Reference "
                        "positions identify the source, not applicability or completeness. "
                        "Ordinary Markdown, inline code, tables and fences are generated content. "
                        "Preserve the source's objects, conditions, exceptions and explanations."
                    ),
                    "blocks": [
                        {
                            k: (v[:64] if k == "opening" else v)
                            for k, v in block.items()
                            if k != "closing"
                        }
                        for block in self.catalog
                    ]
                    if compact
                    else self.catalog,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

    def messages(self, messages: list[dict], *, structured=False, compact=False) -> list[dict]:
        message = self.reference_message() if structured else self.message(compact=compact)
        return messages if message in messages else [*messages, message]

    def reference_message(self) -> dict:
        return {
            "role": "user",
            "content": "Structured original reference catalog:\n"
            + json.dumps(
                {
                    "protocol": "original-references-v1",
                    "interpretation": (
                        legacy_prompts()["excerpt_references"]
                        if self.request_version == 1
                        else "Select supplied reference IDs in excerpt parts; the program copies "
                        "the original bytes and records source positions. References establish "
                        "transcription, not applicability or a complete procedure. Prose parts "
                        "allow normal Markdown including examples. Legacy derived_template "
                        "parts are labelled synthesis with resolvable source references."
                    ),
                    "references": [
                        {
                            "id": key,
                            **self.index.locator(match),
                            "opening": self.index.body(match).splitlines()[0][:120],
                        }
                        for key, match in self.references.items()
                    ],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }

    def _reference(self, identity, **where):
        match = self.references.get(identity)
        if match is None:
            raise problem(
                "excerpt_unknown_reference",
                "Unknown original excerpt ID",
                reference_id=identity,
                **where,
            )
        return match

    @staticmethod
    def _fence(body):
        fence = "`" * max(3, 1 + max((len(s) for s in re.findall(r"`+", body)), default=0))
        return f"\n\n{fence}text\n{body}\n{fence}\n\n"

    def render(self, content: str, *, field_path="$.content") -> str:
        tokens, offsets = markdown_layout(content)
        replacements = []
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
        # Ordinary code, including reference-syntax examples, has no copy contract.
        visible = content
        for token in reversed(tokens):
            if token.map and token.type in {"fence", "code_block"}:
                start, end = offsets[token.map[0]], offsets[token.map[1]]
                visible = visible[:start] + " " * (end - start) + visible[end:]
        markers = list(_MARKER.finditer(visible))
        if _PREFIX in _MARKER.sub("", visible):
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
            source = self._reference(match.group(1), path=field_path)
            body = self.index.body(source)
            self.resolutions.append(self.index.record(source, body, path=field_path))
            replacements.append((match.start(), match.end(), self._fence(body)))
        rendered = content
        # Inserted originals are data, never another round of reference commands.
        for start, end, replacement in reversed(replacements):
            rendered = rendered[:start] + replacement + rendered[end:]
        return "\n\n" + rendered.strip("\n") + "\n\n" if markers else rendered

    def response(
        self,
        raw: str,
        protocol: str,
        expected_sections=(),
        *,
        update=False,
        previous_description: str | None = None,
    ) -> str:
        self.resolutions.clear()
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
        elif protocol in {"summary-json", "summary-parts-json"}:
            value = parse_summary(
                raw,
                expected_sections,
                protocol=protocol if protocol == "summary-parts-json" else None,
            )
            bodies = value["sections"]
        else:
            value = parse_page(raw, strict=protocol == "summary-page-json")
            bodies = [value]
        for field in ("description", "type", "reason"):
            if isinstance(value.get(field), str) and _PREFIX in value[field]:
                raise PageResponseError("page_excerpt_invalid", "Metadata cannot contain excerpts")
        for index, item in enumerate(bodies):
            field = (
                f"$.sections[{index}]"
                if protocol in {"summary-json", "summary-parts-json"}
                else f"$.edits[{index}]"
                if update
                else "$"
            )
            if protocol in {"summary-json", "summary-parts-json"} and "parts" in item:
                item["content"] = self._parts(item.pop("parts"), field)
                continue  # Copied originals are not reinterpreted as response commands.
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                item["content"] = self.render(item["content"], field_path=field + ".content")
        return json.dumps(value, ensure_ascii=False)

    def _parts(self, parts, field):
        bodies = []
        for number, part in enumerate(parts):
            path = f"{field}.parts[{number}]"
            refs = [
                self._reference(identity, path=f"{path}.refs[{index}]")
                for index, identity in enumerate(part["refs"])
            ]
            if part["kind"] == "prose":
                bodies.append(self.render(part["text"], field_path=path + ".text"))
            elif part["kind"] == "excerpt":
                for ref in refs:
                    body = self.index.body(ref)
                    bodies.append(self._fence(body))
                    self.resolutions.append(self.index.record(ref, body, path=path + ".refs"))
            else:
                originals = [self.index.body(ref) for ref in refs]
                locations = [
                    {"id": identity, **self.index.locator(ref)}
                    for identity, ref in zip(part["refs"], refs)
                ]
                self.resolutions.append(
                    {"mode": "derived_template", "path": path + ".text", "references": locations}
                )
                bodies.append(
                    f"推导路径模板 / Derived path template: `{part['text']}`.\n\n"
                    + "\n\n".join(self._fence(body) for body in originals)
                )
        return "\n\n".join(bodies)
