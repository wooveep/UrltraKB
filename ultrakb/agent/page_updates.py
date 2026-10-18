"""Temporary Markdown block edits; this module computes pages and performs no I/O."""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass

from ultrakb import frontmatter
from ultrakb.agent.page_response import PageResponseError, load_protocol_json, parse_page
from ultrakb.content_blocks import markdown_layout


def _literal_blocks(parts: list[str]) -> Counter[str]:
    return Counter(
        token.content
        for part in parts
        for token in markdown_layout(part)[0]
        if token.type in {"fence", "code_block"}
    )


@dataclass(frozen=True)
class UnchangedPage:
    base_sha256: str
    reason: str


class PageUpdate:
    def __init__(
        self,
        original: str,
        *,
        known_targets: set[str] | None = None,
        allowed_types: frozenset[str] | None = None,
        allowed_blocks: frozenset[str] | None = None,
        allow_metadata: bool = True,
        source_ranges: tuple[str, ...] | None = None,
        preserved_fragments: tuple[str, ...] = (),
    ) -> None:
        self.original = original
        self.known_targets = known_targets
        self.allowed_types = allowed_types
        self.allowed_blocks = allowed_blocks
        self.allow_metadata = allow_metadata
        self.source_ranges = source_ranges
        self.preserved_fragments = preserved_fragments
        self.digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
        parts = frontmatter.split(original)
        self.body = parts[1] if parts else original
        if source_ranges is not None:
            from ultrakb.agent.page_figures import split_gallery

            # The application preserves versioned galleries after applying body edits.
            # Their serialized records are not editable Markdown blocks.
            self.body, _ = split_gallery(self.body)
        if any(
            not isinstance(text, str) or not text or text not in self.body
            for text in preserved_fragments
        ):
            raise ValueError("Preserved fragments must occur in the captured page body")
        tokens, offsets = markdown_layout(self.body)
        spans = sorted(
            {
                (offsets[token.map[0]], offsets[token.map[1]])
                for token in tokens
                if token.level == 0 and token.map
            }
        )
        self.blocks = {f"b{ordinal}": span for ordinal, span in enumerate(spans, 1)}

    def request(self) -> dict:
        """Expose request-local anchors, not persistent Wiki identifiers."""
        return {
            "base_sha256": self.digest,
            "description": frontmatter.parse(self.original).get("description", ""),
            "type": frontmatter.parse(self.original).get("type", ""),
            **(
                {"allowed_types": sorted(self.allowed_types)}
                if self.allowed_types is not None
                else {}
            ),
            **(
                {"preserved_fragments": list(self.preserved_fragments)}
                if self.preserved_fragments
                else {}
            ),
            "blocks": [
                {"id": identity, "text": self.body[start:end]}
                for identity, (start, end) in self.blocks.items()
                if self.allowed_blocks is None or identity in self.allowed_blocks
            ],
        }

    def message(self) -> dict:
        return {
            "role": "user",
            "content": "Temporary editable page blocks:\n"
            + json.dumps(self.request(), ensure_ascii=False)
            + "\n\nReturn one JSON object. For changes use status=updated, base_sha256, "
            "optional description/type, and edits. Omit page metadata unless the supplied "
            "originals establish its applicability to the whole page. Omission or the original "
            "value proposes no metadata change. Metadata-only changes may use empty edits. "
            "If the supplied originals "
            "show that the proposed contribution is already covered or needs only navigation, "
            "use only status=unchanged, base_sha256 and a nonempty reason explaining this "
            "decision for the proposed contribution. Unread necessary content cannot establish "
            "unchanged. The captured page is an editable draft, not verified evidence. "
            "Each edit has block_id, operation (replace, insert_before, "
            "insert_after, delete), and content (omit content for delete). Include "
            "complete Markdown paragraphs with their required separators. Incorporate "
            "all necessary new knowledge across any number of blocks. Use the supplied "
            "original contexts to support changes, deletions and wider applicability. "
            "Retain source conditions outside these contexts. When an existing statement "
            "cannot be uniquely targeted, insert a separately attributed paragraph. "
            "The application retains every unedited byte and appends navigation. "
            "If preserved_fragments are supplied, retain those human additions verbatim, "
            "including when editing a paragraph that contains them. They are annotations, "
            "not original-source evidence.",
        }

    def require_current(self, current: str) -> None:
        """Reject a changed human page before attempting to repair generated edits."""
        if current != self.original:
            raise PageResponseError(
                "page_update_stale", "The captured page changed during generation"
            )

    def apply(self, response: str, *, current: str) -> dict | UnchangedPage:
        self.require_current(current)
        try:
            obj = load_protocol_json(
                response.strip().removeprefix("```json").removesuffix("```").strip()
            )
        except ValueError as error:
            raise PageResponseError(
                "page_update_invalid", "Return a valid block-edit JSON object"
            ) from error
        if not isinstance(obj, dict):
            raise PageResponseError("page_update_invalid", "Use a declared update result object")
        if obj.get("base_sha256") != self.digest:
            raise PageResponseError(
                "page_update_invalid", "base_sha256 must match the captured page"
            )
        status = obj.get("status", "updated")
        if status == "unchanged":
            reason = obj.get("reason")
            if (
                set(obj) != {"status", "base_sha256", "reason"}
                or not isinstance(reason, str)
                or not reason.strip()
            ):
                raise PageResponseError(
                    "page_update_invalid", "unchanged requires only status, base_sha256 and reason"
                )
            return UnchangedPage(self.digest, reason)
        if status != "updated" or set(obj) - {
            "status",
            "base_sha256",
            "description",
            "type",
            "edits",
            "source_ranges",
        }:
            raise PageResponseError("page_update_invalid", "Use the declared block-edit fields")
        if self.source_ranges is not None:
            from ultrakb.agent.page_figures import validate_ranges

            validate_ranges(obj, self.source_ranges)
        for key in ("description", "type"):
            if key in obj and not isinstance(obj[key], str):
                raise PageResponseError("page_update_invalid", f"{key} must be a string")
        if "type" in obj and self.allowed_types is not None:
            # Published frontmatter uses title case; configuration uses lower case.
            # Echoing the captured value is not a page-wide metadata contribution.
            value = obj["type"].strip().lower()
            if value == self.request()["type"].strip().lower():
                obj.pop("type")
            elif value in self.allowed_types:
                obj["type"] = value
            else:
                raise PageResponseError(
                    "page_update_invalid",
                    f"type must belong to the allowed set: {sorted(self.allowed_types)}",
                )
        edits = obj.get("edits")
        metadata_changed = any(
            key in obj and obj[key] != self.request()[key] for key in ("description", "type")
        )
        if metadata_changed and not self.allow_metadata:
            raise PageResponseError(
                "page_update_invalid", "Metadata is outside this editing target"
            )
        if not isinstance(edits, list) or (not edits and not metadata_changed):
            raise PageResponseError("page_update_invalid", "A necessary update requires edits")
        by_block: dict[str, list[dict]] = {}
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) - {"block_id", "operation", "content"}:
                raise PageResponseError("page_update_invalid", "Malformed block edit")
            identity, operation = edit.get("block_id"), edit.get("operation")
            if not isinstance(identity, str) or identity not in self.blocks:
                raise PageResponseError("page_update_invalid", "Choose an existing block_id")
            if self.allowed_blocks is not None and identity not in self.allowed_blocks:
                raise PageResponseError(
                    "page_update_invalid", "Target was not supplied for editing"
                )
            if not isinstance(operation, str) or operation not in {
                "replace",
                "insert_before",
                "insert_after",
                "delete",
            }:
                raise PageResponseError("page_update_invalid", "Unknown edit operation")
            if operation != "delete" and not isinstance(edit.get("content"), str):
                raise PageResponseError("page_update_invalid", "Edit content must be a string")
            if self.known_targets is not None and operation != "delete":
                from ultrakb.lint import strip_ghost_wikilinks

                edit = {
                    **edit,
                    "content": strip_ghost_wikilinks(edit["content"], self.known_targets)[0],
                }
            by_block.setdefault(identity, []).append(edit)
        from ultrakb.agent.page_assembly import assemble_blocks

        chunks, cursor = [], 0
        for identity, (start, end) in self.blocks.items():
            changes = by_block.get(identity, [])
            replacements = [e for e in changes if e["operation"] in {"replace", "delete"}]
            if len(replacements) > 1 or (
                any(e["operation"] == "delete" for e in changes) and len(changes) > 1
            ):
                raise PageResponseError(
                    "page_update_conflict", "Conflicting edits target one block"
                )
            chunks.append((self.body[cursor:start], False))
            chunks.extend(
                (e["content"], True) for e in changes if e["operation"] == "insert_before"
            )
            replacement = replacements[0] if replacements else None
            chunks.append(
                (
                    (
                        replacement.get("content", "")
                        if replacement["operation"] == "replace"
                        else ""
                    )
                    if replacement
                    else self.body[start:end],
                    replacement is not None,
                )
            )
            chunks.extend((e["content"], True) for e in changes if e["operation"] == "insert_after")
            cursor = end
        chunks.append((self.body[cursor:], False))
        newline = "\r\n" if "\r\n" in self.body else "\n"
        content = assemble_blocks(chunks, newline=newline)
        literals = _literal_blocks([text for text, _ in chunks])
        if literals != _literal_blocks([content]):
            content = assemble_blocks(chunks, newline=newline, separate=True)
        page = {
            "description": obj.get("description", self.request()["description"]),
            "content": content,
        }
        missing = [
            text
            for text in self.preserved_fragments
            if page["content"].count(text) < self.body.count(text)
        ]
        if missing:
            raise PageResponseError(
                "page_manual_content_removed",
                "Preserve the recorded manual additions verbatim: "
                + json.dumps(missing, ensure_ascii=False),
            )
        if literals != _literal_blocks([page["content"]]):
            raise PageResponseError(
                "page_assembly_invalid",
                "Page assembly changed a literal block; keep complete Markdown separators",
            )
        if self.source_ranges is not None:
            page["source_ranges"] = obj["source_ranges"]
        if "type" in obj:
            page["type"] = obj["type"]
        return parse_page(json.dumps(page, ensure_ascii=False))
