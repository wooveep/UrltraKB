"""Page-level reading advice becomes bounded, frozen original compile context."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from openkb import frontmatter
from openkb.agent.compile_evidence import MAX_EVIDENCE_CHARS, MAX_EVIDENCE_UNITS
from openkb.agent.page_response import PageResponseError

READING_PLAN_INSTRUCTION = """
For long-document pages, optionally include reading_locations with node_ids
(existing navigation IDs), pages (physical ordinals), blocks (native ordinals),
cells (worksheet A1 range), or chars (0-based START:END). Give only relevant
page-level locations, not per-claim evidence. The application validates and
reads bounded windows. Without a relevant original window, propose only a
navigation brief, not specific values, commands or operating rules.
Use an update only for a new or revised contribution from the current source;
an existing inventory entry alone does not establish an editing need. Confirm
the relevant original location, or withdraw an unnecessary editing proposal.
Examples: "reading_locations": {"pages": "2,4-6"}, or
"reading_locations": {"node_ids": ["0001"]}. Pages/blocks may also be an
array of 1-6 unique positive ordinals. Cells/chars must be range strings.
"""


@dataclass(frozen=True)
class PageOriginalContext:
    message: dict
    has_original: bool
    has_current_original: bool = False
    windows: tuple[dict, ...] = ()

    def require_update(self, page: str) -> None:
        if not self.has_current_original:
            from openkb.compilation_report import CompilationIncomplete, report_compile_issue

            code = "page_update_originals_unread"
            report_compile_issue(code, page)
            raise CompilationIncomplete(
                (code,), (page,), "Existing page retained; locate relevant current originals first"
            )

    def navigation_page(self, title: str, doc_name: str) -> tuple[str, str]:
        return (
            f"Navigation: {title}; original details have not been read",
            f"# {title}\n\nNavigation entry for [[summaries/{doc_name}]]. "
            "No relevant original window was located for this page. "
            "Read the original source before using specific values or operating rules.\n",
        )


def navigation_nodes(structure: list[dict]) -> list[dict]:
    result = []
    for node in structure:
        result.append(node)
        result.extend(navigation_nodes(node.get("nodes", [])))
    return result


def validate_reading_locations(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {
        "node_ids",
        "pages",
        "blocks",
        "cells",
        "chars",
    }:
        raise ValueError("reading_locations requires native ranges or existing node_ids")
    if "node_ids" in value:
        ids = value["node_ids"]
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 6
            or any(not isinstance(n, str) or not n for n in ids)
        ):
            raise ValueError("reading_locations.node_ids requires 1-6 navigation IDs")
    for key in set(value) - {"node_ids"}:
        if key in {"pages", "blocks"} and isinstance(value[key], list):
            numbers = value[key]
            if (
                not 1 <= len(numbers) <= MAX_EVIDENCE_UNITS
                or any(type(n) is not int or n < 1 for n in numbers)
                or len(set(numbers)) != len(numbers)
            ):
                raise ValueError(f"reading_locations.{key} requires 1-6 unique positive ordinals")
            value = {**value, key: ",".join(map(str, numbers))}
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"reading_locations.{key} requires a nonempty range")
    if sum(key in value for key in ("pages", "blocks", "cells", "chars")) > 1:
        raise ValueError("reading_locations must use one native coordinate system")
    return value


class CompileSourceWindows:
    def __init__(
        self,
        wiki: Path,
        doc_name: str,
        *,
        navigation: list[dict] | None = None,
        kb_dir: Path | None = None,
        view_id: str = "legacy",
    ):
        self.wiki = wiki.resolve()
        self.doc_name = doc_name
        self.navigation = navigation_nodes(navigation or [])
        self.kb_dir, self.view_id = kb_dir, view_id
        self._frozen: dict[str, bytes] = {}
        self._searches: dict = {}
        self._unresolved_updates: set[str] = set()

    def validate_updates(self, plan, *, long: bool) -> str | None:
        """Diagnose editing intent before page generation, within the planner's retry."""
        if not long:
            return None
        unresolved = []
        remaining: set[str] = set()
        for kind in ("concepts", "entities"):
            group = getattr(plan, kind)
            for item in group.update:
                page = f"{kind}/{item['name']}.md"
                remaining.add(page)
                context = self.prepare(item, long=True)
                if not context.has_current_original:
                    self._unresolved_updates.add(page)
                    metadata = frontmatter.parse(self._read(page).decode("utf-8"))
                    unresolved.append(
                        {
                            "page": page,
                            "proposal": item,
                            "existing_description": metadata.get("description", ""),
                            "windows": context.windows,
                        }
                    )
            for name in group.related:
                page = f"{kind}/{name}.md"
                if page in self._unresolved_updates:
                    remaining.add(page)
                    unresolved.append(
                        {
                            "page": page,
                            "diagnostic": (
                                "An unresolved update cannot become a new source "
                                "contribution via related."
                            ),
                        }
                    )
        if unresolved:
            return (
                "Confirm current-source editing intent for unresolved updates. "
                "Return a corrected complete concepts/entities plan. Supply relevant native "
                "reading_locations for necessary updates, or withdraw unnecessary proposals "
                "by omitting them. Keep all necessary new knowledge and valid proposals. "
                "No-match diagnostics do not establish that the source lacks knowledge. "
                "Unresolved necessary updates remain blocked. Diagnostics:\n"
                + json.dumps(unresolved, ensure_ascii=False)
            )
        from openkb.compilation_report import report_compile_issue

        if self._unresolved_updates - remaining:
            report_compile_issue("compile_plan_update_withdrawn")
        return None

    def _read(self, path: str) -> bytes:
        target = (self.wiki / path).resolve()
        if not target.is_relative_to(self.wiki):
            raise ValueError("Compile source escapes its selected knowledge collection")
        if path not in self._frozen:
            self._frozen[path] = target.read_bytes()
        return self._frozen[path]

    def _originals(self, page: str) -> list[str]:
        pending, seen, sources = [page], set(), []
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            if path.startswith("sources/") and path.endswith((".md", ".json")):
                # Retain the richer native map where one exists.
                mapped = path.removesuffix(".md") + ".content.json"
                sources.append(
                    mapped if path.endswith(".md") and (self.wiki / mapped).is_file() else path
                )
                continue
            metadata = frontmatter.parse(self._read(path).decode("utf-8"))
            links = metadata.get("sources", [])
            if not isinstance(links, list) or any(not isinstance(n, str) for n in links):
                raise ValueError("Invalid compile source associations")
            pending.extend(links)
            if pointer := metadata.get("full_text"):
                if not isinstance(pointer, str):
                    raise ValueError("Invalid summary original pointer")
                pending.append(pointer)
        return sorted(set(sources))

    def _path(self) -> str | None:
        for suffix in (".content.json", ".json", ".md"):
            path = f"sources/{self.doc_name}{suffix}"
            if (self.wiki / path).is_file():
                return path
        return None

    def _locations(
        self, item: dict, *, path: str | None = None, navigation: list[dict] | None = None
    ) -> dict:
        advice = validate_reading_locations(item.get("reading_locations", {}))
        navigation = self.navigation if navigation is None else navigation_nodes(navigation)
        nodes = {str(n.get("node_id")): n for n in navigation}
        requested = advice.get("node_ids", [])
        if any(n not in nodes for n in requested):
            raise PageResponseError("page_invalid_evidence", "Unknown navigation node")
        if not advice:
            from openkb.source_search import SourceSearch, search_units

            source_path = path or self._path()
            terms = [
                item.get("title", ""),
                item["name"].replace("-", " "),
                item.get("brief", ""),
                *item.get("aliases", []),
            ]
            if source_path:
                if source_path not in self._searches:
                    data = self._read(source_path)
                    raw = (
                        json.loads(data) if source_path.endswith(".json") else data.decode("utf-8")
                    )
                    self._searches[source_path] = SourceSearch(raw)
                hits = search_units(self._searches[source_path], terms)
                locations = [hit["location"] for hit in hits[:MAX_EVIDENCE_UNITS]]
                if locations:
                    for key in ("pages", "blocks"):
                        if key in locations[0]:
                            return {key: ",".join(dict.fromkeys(loc[key] for loc in locations))}
                    spans = [tuple(map(int, loc["chars"].split(":"))) for loc in locations]
                    return {"chars": f"{min(a for a, _ in spans)}:{max(b for _, b in spans)}"}
            words = set(
                re.findall(
                    r"[\w]+",
                    (item.get("title", "") + " " + item["name"].replace("-", " ")).casefold(),
                )
            )
            scored = [
                (
                    3 * len(words & set(re.findall(r"[\w]+", str(n.get("title", "")).casefold())))
                    + len(
                        words
                        & set(
                            re.findall(
                                r"[\w]+",
                                str(n.get("summary", "")).casefold(),
                            )
                        )
                    ),
                    n,
                )
                for n in navigation
            ]
            scored.sort(key=lambda pair: pair[0], reverse=True)
            if scored and scored[0][0]:
                requested = [str(node.get("node_id")) for score, node in scored if score > 0][
                    :MAX_EVIDENCE_UNITS
                ]
        if requested and not any(k in advice for k in ("pages", "blocks", "cells", "chars")):
            numbers: set[int] = set()
            for identity in requested:
                node = nodes[identity]
                start, end = node.get("start_index"), node.get("end_index")
                if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                    raise PageResponseError("page_invalid_evidence", "Invalid navigation range")
                numbers.update(range(start, min(end, start + MAX_EVIDENCE_UNITS - 1) + 1))
            source_path = path or self._path() or ""
            key = "blocks" if source_path.endswith(".content.json") else "pages"
            return {key: ",".join(map(str, sorted(numbers)[:MAX_EVIDENCE_UNITS]))}
        return {k: v for k, v in advice.items() if k != "node_ids"}

    def _window(self, path: str, location: dict, remaining: int) -> dict:
        data = self._read(path)
        result: dict = {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "coverage": "partial",
            "content": "",
            "images": [],
        }
        if path.endswith(".content.json"):
            from openkb.agent.evidence_payload import project_evidence
            from openkb.block_package import read_block_selection
            from openkb.text_source import read_text_selection
            from openkb.workbooks.selection import select_cells

            raw = json.loads(data)
            if "pages" in location:
                raise PageResponseError(
                    "page_invalid_evidence", "This source has no physical pages"
                )
            if "blocks" in location:
                selected = read_block_selection(raw, location["blocks"])
            else:
                selected = read_text_selection(raw, location.get("chars"))
                if "cells" in location:
                    selected = select_cells(selected, location["cells"])
            projected = project_evidence(selected, budget=remaining)
            result.update(projected)
            result["native_location"] = location or {"chars": f"0:{len(raw['text'])}"}
        elif path.endswith(".json"):
            from openkb.source_pages import read_page_selection

            raw = json.loads(data)
            if set(location) - {"pages"}:
                raise PageResponseError("page_invalid_evidence", "Use this source's physical pages")
            if "pages" not in location:
                return {**result, "diagnostics": ["No relevant original pages located"]}
            selected = read_page_selection(raw, location["pages"])
            if selected["diagnostics"]:
                raise PageResponseError("page_invalid_evidence", str(selected["diagnostics"]))
            content, numbers, images = "", [], []
            for unit in selected["units"][:MAX_EVIDENCE_UNITS]:
                piece = f"[Physical page {unit['page']}]\n{unit['content']}\n"
                if len(content) + len(piece) > remaining:
                    break
                content += piece
                numbers.append(unit["page"])
                images.extend(unit.get("images", []))
            result.update(
                content=content,
                images=images,
                native_location={"physical pages": numbers},
                coverage="complete" if numbers == [p["page"] for p in raw] else "partial",
            )
        else:
            text = data.decode("utf-8")
            start, end = 0, len(text)
            if "chars" in location:
                start, end = map(int, location["chars"].split(":"))
                if not 0 <= start < end <= len(text):
                    raise PageResponseError("page_invalid_evidence", "Invalid original text range")
            # Keep complete paragraphs and make any uncovered suffix explicit.
            paragraphs, content = text[start:end].splitlines(keepends=True), ""
            for paragraph in paragraphs:
                if len(content) + len(paragraph) > remaining:
                    break
                content += paragraph
            result.update(
                content=content,
                native_location={"chars": f"{start}:{start + len(content)}"},
                coverage="complete" if content == text else "partial",
                range_complete=len(content) == end - start,
                continuation=(
                    {"chars": f"{start + len(content)}:{end}"}
                    if len(content) < end - start
                    else None
                ),
            )
        return result

    def prepare(
        self, item: dict, previous_page: str | None = None, *, long: bool = False
    ) -> PageOriginalContext:
        paths: dict[str, dict] = {}
        if long and (path := self._path()):
            paths[path] = self._locations(item)
        if previous_page and (self.wiki / previous_page).is_file():
            for path in self._originals(previous_page):
                location = {}
                if path.endswith(".json") and not path.endswith(".content.json") and self.kb_dir:
                    from openkb.source_navigation import published_navigation

                    previous = published_navigation(self.kb_dir, self.view_id, Path(path).stem)
                    location = self._locations(
                        {"name": item["name"], "title": item.get("title", "")},
                        path=path,
                        navigation=previous.get("structure", []),
                    )
                paths.setdefault(path, location)
        windows: list[dict] = []
        remaining = MAX_EVIDENCE_CHARS
        for path, location in paths.items():
            window: dict
            if not remaining:
                window = {"path": path, "coverage": "unread", "content": ""}
            else:
                try:
                    window = self._window(path, location, remaining)
                except FileNotFoundError:
                    window = {
                        "path": path,
                        "coverage": "unread",
                        "content": "",
                        "diagnostics": ["Linked original unavailable; do not infer absence"],
                    }
            if not window["content"].strip():
                window["coverage"] = "unread"
            remaining -= len(window["content"])
            window["source_role"] = "current" if path == self._path() else "prior_linked_source"
            window["unread"] = window["coverage"] != "complete"
            window["reading_action"] = {
                "source_path": path,
                "native_location": location,
                "continuation": window.get("continuation"),
                "instruction": "Locate or extend this source window before "
                "reassessing uncovered claims",
            }
            windows.append(window)
        prior_evidence = {}
        if previous_page and self.kb_dir:
            from openkb.knowledge_evidence import page_evidence
            from openkb.knowledge_scope import live_scope

            prior_evidence = page_evidence(live_scope(self.kb_dir, self.view_id), previous_page)
        blocks: list[dict] = [
            {
                "type": "text",
                "text": (
                    "Current short-document original is supplied in full in the earlier "
                    "document message. The windows below supplement it with linked sources.\n"
                    if not long
                    else ""
                )
                + "Original source windows (bounded, frozen):\n"
                + json.dumps(windows, ensure_ascii=False)
                + "\nPrior page basis (retain these source revisions and any pending refresh): "
                + json.dumps(prior_evidence, ensure_ascii=False)
                + f"\nWindow capacity remaining: {remaining} characters. "
                + "\nCoverage describes only these reads. Prior generated pages are "
                "drafts, not evidence. Unread claims are not verified. "
                + (
                    "For a NEW page without relevant originals, produce only a navigation brief. "
                    if long
                    else "An empty supplementary list does not remove the current full original. "
                )
                + "For an existing page, a missing or partial window is not evidence of error. "
                "Reassess only the supplied source ranges. Retain supported content and source "
                "conditions outside those ranges; do not claim unread material was reverified. "
                "Correct explicit counterevidence or demonstrably unsupported generated claims "
                "within the covered scope. Preserve supported calculations and labelled synthesis. "
                "A changed source leaves old evidence pending refresh; never relabel it as the "
                "new source revision. Do not transfer one source's defaults or guarantees "
                "to another. "
                + "Keep OCR transcriptions separate from generated visual interpretations.",
            }
        ]
        from openkb.agent.tools import read_wiki_image
        from openkb.markdown_images import image_references

        seen: set[str] = set()
        for window in windows:
            entries = list(window.get("images", []))
            assets = window.get("assets", {})
            for reference in image_references(window["content"]):
                if path := reference.local_path:
                    if path not in assets:
                        path = f"sources/{path}"
                    if path in assets:
                        entries.append({"path": path, "digest": assets[path]})
            for entry in entries:
                image_path = entry["path"]
                if image_path in seen:
                    continue
                if len(seen) >= 4:
                    blocks.append(
                        {
                            "type": "text",
                            "text": f"Original image unread (image limit): {image_path}",
                        }
                    )
                    continue
                seen.add(image_path)
                data = self._read(image_path)
                digest = hashlib.sha256(data).hexdigest()
                if entry.get("digest") and entry["digest"] != digest:
                    raise PageResponseError(
                        "page_evidence_changed", "Frozen original image changed"
                    )
                if len(data) <= 5_000_000:
                    image = read_wiki_image(image_path, str(self.wiki), expected_digest=digest)
                    if image["type"] == "image":
                        blocks.extend(
                            [
                                {"type": "text", "text": f"Original image: {image_path}"},
                                {"type": "image_url", "image_url": {"url": image["image_url"]}},
                            ]
                        )
                    else:
                        raise PageResponseError("page_invalid_evidence", image["text"])
                else:
                    blocks.append(
                        {
                            "type": "text",
                            "text": f"Original image unread (size limit): {image_path}",
                        }
                    )
        return PageOriginalContext(
            {"role": "user", "content": blocks},
            any(w["content"].strip() for w in windows),
            not long or any(w["path"] == self._path() and w["content"].strip() for w in windows),
            tuple(windows),
        )
