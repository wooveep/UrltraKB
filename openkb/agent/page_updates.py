"""Temporary Markdown block edits; this module computes pages and performs no I/O."""

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, Inexact, localcontext

from openkb import frontmatter
from openkb.agent.page_response import PageResponseError, parse_page
from openkb.content_blocks import markdown_layout

_UNITS = {
    "ms": ("time", Decimal("0.001")),
    "s": ("time", Decimal(1)),
    "秒": ("time", Decimal(1)),
    "min": ("time", Decimal(60)),
    "h": ("time", Decimal(3600)),
    "小时": ("time", Decimal(3600)),
    "g": ("mass", Decimal("0.001")),
    "kg": ("mass", Decimal(1)),
    "mL": ("volume", Decimal("0.001")),
    "L": ("volume", Decimal(1)),
    "升": ("volume", Decimal(1)),
}
_OBJECT_HEADERS = {"object", "对象", "id", "name", "名称", "model", "型号"}
_CONDITION_HEADERS = {"condition", "条件", "适用条件", "scope", "适用范围"}
_BINDING_HEADERS = _OBJECT_HEADERS | _CONDITION_HEADERS | {"attribute", "属性", "version", "版本"}


def _quantity(text: str, header: str = "") -> tuple[str, Decimal] | None:
    match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*([^\d\s]*)", text.strip())
    if not match:
        return None
    unit = match[2]
    if not unit and (hint := re.search(r"[（(]([^（）()]+)[）)]$", header)):
        unit = hint[1]
    if unit and unit not in _UNITS:
        return None
    dimension, factor = _UNITS.get(unit, ("", Decimal(1)))
    return dimension, Decimal(match[1]) * factor


def _numeric_facts(text: str) -> list[tuple[tuple, tuple[str, Decimal]]]:
    tokens, _ = markdown_layout(text)
    headings: list[tuple[int, str]] = []
    facts = []
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token.type == "heading_open":
            level = int(token.tag[1:])
            headings = [h for h in headings if h[0] < level]
            headings.append((level, tokens[position + 1].content))
        scope = tuple(title for _, title in headings)
        if token.type == "table_open":
            rows: list[list[str]] = []
            for following in tokens[position + 1 :]:
                if following.type == "table_close":
                    break
                if following.type == "tr_open":
                    rows.append([])
                elif following.type == "inline" and rows:
                    rows[-1].append(following.content.strip())
            if rows:
                headers = rows[0]
                for row in rows[1:]:
                    if len(row) != len(headers):
                        continue
                    values = [
                        None if header.casefold() in _BINDING_HEADERS else _quantity(value, header)
                        for header, value in zip(headers, row)
                    ]
                    bindings = tuple(
                        sorted(
                            (header, value)
                            for header, value, quantity in zip(headers, row, values)
                            if quantity is None
                        )
                    )
                    labels = {header.casefold() for header, _ in bindings}
                    if not labels & _OBJECT_HEADERS or not labels & _CONDITION_HEADERS:
                        continue
                    for header, quantity in zip(headers, values):
                        if quantity is not None:
                            facts.append((("table", scope, bindings, header), quantity))
        elif token.type == "inline" and token.level == 1:
            pairs = []
            for line in token.content.splitlines():
                if match := re.fullmatch(r"([^:：|]+)[:：]\s*(.+)", line):
                    pairs.append((match[1].strip(), match[2].strip()))
            bindings = tuple(
                sorted(
                    (key, value)
                    for key, value in pairs
                    if key.casefold() in _BINDING_HEADERS or _quantity(value) is None
                )
            )
            labels = {key.casefold() for key, _ in bindings}
            if labels & _OBJECT_HEADERS and labels & _CONDITION_HEADERS:
                facts.extend(
                    (("kv", scope, bindings, key), quantity)
                    for key, value in pairs
                    if key.casefold() not in _BINDING_HEADERS
                    and (quantity := _quantity(value)) is not None
                )
        position += 1
    return facts


def _arithmetic(expression: str) -> Decimal | None:
    if len(expression) > 120:
        return None
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        if len(list(ast.walk(tree))) > 64:
            return None

        def value(node) -> Decimal:
            if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
                literal = ast.get_source_segment(expression.strip(), node)
                if literal is None:
                    raise ValueError("Missing numeric literal")
                return Decimal(literal)
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                number = value(node.operand)
                return -number if isinstance(node.op, ast.USub) else number
            if isinstance(node, ast.BinOp):
                left, right = value(node.left), value(node.right)
                if isinstance(node.op, ast.Add):
                    return left + right
                if isinstance(node.op, ast.Sub):
                    return left - right
                if isinstance(node.op, ast.Mult):
                    return left * right
                if isinstance(node.op, ast.Div):
                    return left / right
                if isinstance(node.op, ast.Pow) and right == int(right) and abs(right) <= 12:
                    return left ** int(right)
            raise ValueError("Unsupported arithmetic expression")

        with localcontext() as context:
            context.prec = 80
            context.traps[Inexact] = True
            return value(tree.body)
    except (ValueError, SyntaxError, ArithmeticError):
        return None


def check_numeric_subset(candidate: str, originals: list[str], *, previous: str = "") -> dict:
    """Only compare unambiguous structured bindings; unmatched prose remains unchecked."""
    reference: dict[tuple, set[tuple[str, Decimal]]] = {}
    for original in originals:
        for key, value in _numeric_facts(original):
            reference.setdefault(key, set()).add(value)
    old: dict[tuple, set[tuple[str, Decimal]]] = {}
    for key, value in _numeric_facts(previous):
        old.setdefault(key, set()).add(value)
    conflicts, checked = [], 0
    for key, value in _numeric_facts(candidate):
        values = reference.get(key, set())
        if value in old.get(key, set()) or len(values) != 1:
            continue
        expected = next(iter(values))
        if value[0] != expected[0] and (not value[0] or not expected[0]):
            continue  # An implicit/unknown unit cannot prove a dimension conflict.
        checked += 1
        if value != expected:
            conflicts.append({"attribute": key[-1], "reason": "structured_value_conflict"})
    for line in candidate.splitlines():
        equation = re.fullmatch(r"\s*([0-9.eE\s()+*/-]+)\s*=\s*([+-]?\d+(?:\.\d+)?)\s*", line)
        if not equation or line in previous.splitlines():
            continue
        result = _arithmetic(equation[1])
        if result is None:
            continue
        checked += 1
        if result != Decimal(equation[2]):
            conflicts.append({"reason": "arithmetic_conflict", "expression": equation[1].strip()})
    return {
        "status": "conflict"
        if conflicts
        else "checked_numeric_subset"
        if checked
        else "not_applicable",
        "checked": checked,
        "conflicts": conflicts,
    }


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
        originals: list[str] | None = None,
        allowed_types: frozenset[str] | None = None,
        allowed_blocks: frozenset[str] | None = None,
        allow_metadata: bool = True,
        source_ranges: tuple[str, ...] | None = None,
    ) -> None:
        self.original = original
        self.originals = originals
        self.known_targets = known_targets
        self.allowed_types = allowed_types
        self.allowed_blocks = allowed_blocks
        self.allow_metadata = allow_metadata
        self.source_ranges = source_ranges
        self.digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
        parts = frontmatter.split(original)
        self.body = parts[1] if parts else original
        if source_ranges is not None:
            from openkb.agent.page_figures import split_gallery

            # The application preserves versioned galleries after applying body edits.
            # Their serialized records are not editable Markdown blocks.
            self.body, _ = split_gallery(self.body)
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
            "The application retains every unedited byte and appends navigation.",
        }

    def apply(self, response: str, *, current: str) -> dict | UnchangedPage:
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
            from openkb.agent.page_figures import validate_ranges

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
        if self.source_ranges is not None:
            page["source_ranges"] = obj["source_ranges"]
        if self.originals is not None:
            numeric = check_numeric_subset(page["content"], self.originals, previous=self.body)
            if numeric["status"] == "conflict":
                raise PageResponseError("page_numeric_conflict", json.dumps(numeric["conflicts"]))
        if "type" in obj:
            page["type"] = obj["type"]
        return parse_page(json.dumps(page, ensure_ascii=False))
