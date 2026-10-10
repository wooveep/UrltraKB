"""Temporary Markdown block edits; this module computes pages and performs no I/O."""

import hashlib
import json

from openkb import frontmatter
from openkb.agent.page_response import PageResponseError, parse_page
from openkb.content_blocks import markdown_layout


class PageUpdate:
    def __init__(self, original: str, *, known_targets: set[str] | None = None) -> None:
        self.original = original
        self.known_targets = known_targets
        self.digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
        parts = frontmatter.split(original)
        self.body = parts[1] if parts else original
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
            "blocks": [
                {"id": identity, "text": self.body[start:end]}
                for identity, (start, end) in self.blocks.items()
            ],
        }

    def message(self) -> dict:
        return {
            "role": "user",
            "content": "Temporary editable page blocks:\n"
            + json.dumps(self.request(), ensure_ascii=False)
            + "\n\nReturn one JSON object with base_sha256, description, optional type, "
            "and edits. Each edit has block_id, operation (replace, insert_before, "
            "insert_after, delete), and content (omit content for delete). Include "
            "complete Markdown paragraphs with their required separators. Incorporate "
            "all necessary new knowledge across any number of blocks. Use the supplied "
            "original contexts to support changes, deletions and wider applicability. "
            "Retain source conditions outside these contexts. When an existing statement "
            "cannot be uniquely targeted, insert a separately attributed paragraph. "
            "The application retains every unedited byte and appends navigation.",
        }

    def apply(self, response: str, *, current: str) -> dict:
        if current != self.original:
            raise PageResponseError(
                "page_update_stale", "The captured page changed during generation"
            )
        try:
            obj = json.loads(response.strip().removeprefix("```json").removesuffix("```").strip())
        except ValueError as error:
            raise PageResponseError(
                "page_update_invalid", "Return a valid block-edit JSON object"
            ) from error
        if not isinstance(obj, dict) or set(obj) - {"base_sha256", "description", "type", "edits"}:
            raise PageResponseError("page_update_invalid", "Use the declared block-edit fields")
        if obj.get("base_sha256") != self.digest:
            raise PageResponseError(
                "page_update_invalid", "base_sha256 must match the captured page"
            )
        for key in ("description", "type"):
            if key in obj and not isinstance(obj[key], str):
                raise PageResponseError("page_update_invalid", f"{key} must be a string")
        edits = obj.get("edits")
        if not isinstance(edits, list) or not edits:
            raise PageResponseError("page_update_invalid", "A necessary update requires edits")
        by_block: dict[str, list[dict]] = {}
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) - {"block_id", "operation", "content"}:
                raise PageResponseError("page_update_invalid", "Malformed block edit")
            identity, operation = edit.get("block_id"), edit.get("operation")
            if not isinstance(identity, str) or identity not in self.blocks:
                raise PageResponseError("page_update_invalid", "Choose an existing block_id")
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
                from openkb.lint import strip_ghost_wikilinks

                edit = {
                    **edit,
                    "content": strip_ghost_wikilinks(edit["content"], self.known_targets)[0],
                }
            by_block.setdefault(identity, []).append(edit)
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
            chunks.append(self.body[cursor:start])
            chunks.extend(e["content"] for e in changes if e["operation"] == "insert_before")
            replacement = replacements[0] if replacements else None
            chunks.append(
                (replacement.get("content", "") if replacement["operation"] == "replace" else "")
                if replacement
                else self.body[start:end]
            )
            chunks.extend(e["content"] for e in changes if e["operation"] == "insert_after")
            cursor = end
        chunks.append(self.body[cursor:])
        page = {
            "description": obj.get("description", self.request()["description"]),
            "content": "".join(chunks),
        }
        if "type" in obj:
            page["type"] = obj["type"]
        return parse_page(json.dumps(page, ensure_ascii=False))
