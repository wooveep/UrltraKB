"""Page-level reading advice becomes bounded, frozen original compile context."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

from openkb import frontmatter
from openkb.agent.compile_evidence import MAX_EVIDENCE_CHARS, MAX_EVIDENCE_UNITS
from openkb.agent.compile_messages import render_original_window
from openkb.agent.page_originals import PageOriginalContext as PageOriginalContext
from openkb.agent.page_response import PageResponseError

READING_PLAN_INSTRUCTION = """
For a long document with multiple navigation sections, every create or update
must include reading_locations. Select the smallest original section(s) covering
the proposed topic, including its prerequisites, conditions and procedure steps.
A selected parent includes its descendant sections. A single-section document
may omit reading_locations. Use node_ids
(existing navigation IDs), pages (physical ordinals), blocks (native ordinals),
cells (worksheet A1 range), or chars (0-based START:END). Give only relevant
page-level locations, not per-claim evidence. The application validates and
reads the necessary selection through bounded windows automatically. Supply the
relevant native range; do not guess offsets or drop necessary middle content.
Without a relevant original window, propose only a
navigation brief, not specific values, commands or operating rules.
Use an update only for a new or revised contribution from the current source;
use brief to describe that proposed contribution and its conditions.
an existing inventory entry alone does not establish an editing need. Confirm
the relevant original location, or withdraw an unnecessary editing proposal.
Examples: "reading_locations": {"pages": "2,4-6"}, or
"reading_locations": {"node_ids": ["0001"]}. Pages/blocks may also be an
array of unique positive ordinals. Cells/chars must be range strings.
"""


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
        "offset",
    }:
        raise ValueError("reading_locations requires native ranges or existing node_ids")
    if "node_ids" in value:
        ids = value["node_ids"]
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(n, str) or not n for n in ids)
        ):
            raise ValueError("reading_locations.node_ids requires nonempty navigation IDs")
    if "offset" in value and (
        type(value["offset"]) is not int
        or value["offset"] < 0
        or not set(value) & {"pages", "blocks", "cells", "chars"}
    ):
        raise ValueError("reading_locations.offset requires a nonnegative reader offset and range")
    for key in set(value) - {"node_ids", "offset"}:
        if key in {"pages", "blocks"} and isinstance(value[key], list):
            numbers = value[key]
            if (
                not numbers
                or any(type(n) is not int or n < 1 for n in numbers)
                or len(set(numbers)) != len(numbers)
            ):
                raise ValueError(f"reading_locations.{key} requires unique positive ordinals")
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
        self._parsed: dict[str, object] = {}
        self._selections: dict[str, tuple[dict, dict]] = {}
        self._unresolved_updates: set[str] = set()
        self._numeric_text: dict[str, str] = {}
        self._figure_sources: dict[str, tuple] = {}

    def figure_source(self, path: str):
        from openkb.figure_assets import freeze_figures, source_identity

        if path not in self._figure_sources:
            name = (
                Path(path)
                .name.removesuffix(".content.json")
                .removesuffix(".json")
                .removesuffix(".md")
            )
            inputs = None
            if self.kb_dir is not None and name != self.doc_name:
                from openkb.unit_publication import read_head

                inputs = read_head(self.kb_dir, self.view_id).inputs
            identity = source_identity(self.kb_dir, name, inputs=inputs)
            identity["source_digest"] = hashlib.sha256(self._read(path)).hexdigest()
            figures = freeze_figures(
                self.wiki,
                path,
                self._source(path),
                {k: v for k, v in identity.items() if k != "source_digest"},
            )
            self._figure_sources[path] = (identity, figures)
        return self._figure_sources[path]

    def page_figures(self, windows=(), *, long=False):
        from openkb.agent.page_figures import page_figure_context

        return page_figure_context(self, windows, long=long)

    def numeric_sources(self, context: PageOriginalContext, *, long: bool) -> list[str]:
        """Supply only original text; generated navigation is never a numeric reference."""
        texts = [window["content"] for window in context.windows if window["content"]]
        if not long and (path := self._path()):
            if path not in self._numeric_text:
                data = self._read(path)
                raw = json.loads(data) if path.endswith(".json") else data.decode("utf-8")
                self._numeric_text[path] = (
                    raw
                    if isinstance(raw, str)
                    else raw["text"]
                    if isinstance(raw, dict)
                    else "\n".join(page.get("content", "") for page in raw)
                )
            texts.insert(0, self._numeric_text[path])
        return texts

    def current_original_evidence(self, text: str | None = None) -> dict | None:
        """Project the same qualified original for capacity, compilation and reading."""
        path = self._path()
        if path is None:
            return None
        from openkb.agent.evidence_payload import project_evidence

        selected, location = self._document_selection(path)
        markdown = f"sources/{self.doc_name}.md"
        # Native page/block readers add display labels. Verify the captured
        # normalization bytes, rather than comparing those labels with Markdown.
        normalized = (
            self._source(markdown) if (self.wiki / markdown).is_file() else self._source(path)
        )
        normalized = normalized.get("text") if isinstance(normalized, dict) else normalized
        if text is not None and normalized != text:
            raise PageResponseError(
                "page_evidence_changed", "Current original differs from frozen text"
            )
        projected = render_original_window(
            project_evidence(selected, fits=lambda payload: True), selected
        )
        window = {
            "path": path,
            "sha256": hashlib.sha256(self._read(path)).hexdigest(),
            "native_location": location,
            **projected,
        }
        return window

    def current_original_message(self, text: str | None = None) -> dict | None:
        """Provide the frozen short original independently of the summary task."""
        window = self.current_original_evidence(text)
        if window is None:
            return None
        return {
            "role": "user",
            "content": f"Current original: {window['path']}\n"
            + json.dumps(window, ensure_ascii=False),
        }

    def validate_updates(self, plan, *, long: bool) -> str | None:
        """Diagnose editing intent before page generation, within the planner's retry."""
        if not long:
            return None
        unresolved = []
        remaining: set[str] = set()
        for kind in ("concepts", "entities"):
            group = getattr(plan, kind)
            unscoped = set()
            if len(self.navigation) > 1:
                for item in [*group.create, *group.update]:
                    if not item.get("reading_locations"):
                        page = f"{kind}/{item['name']}.md"
                        unscoped.add(page)
                        unresolved.append(
                            {
                                "page": page,
                                "proposal": item,
                                "diagnostic": "Select relevant native reading_locations for this "
                                "multi-section document, including the proposed topic's "
                                "prerequisites and conditions. Parent node_ids include "
                                "descendants.",
                            }
                        )
            for item in group.update:
                page = f"{kind}/{item['name']}.md"
                remaining.add(page)
                if page in unscoped:
                    self._unresolved_updates.add(page)
                    continue
                context = self.prepare(item, page, long=True)
                current = [w for w in context.windows if w["source_role"] == "current"]
                truncated = bool(current and type(current[-1].get("continuation")) is int)
                fresh_read = bool(current and current[-1]["content"].strip())
                if not fresh_read or truncated:
                    self._unresolved_updates.add(page)
                    metadata = frontmatter.parse(self._read(page).decode("utf-8"))
                    unresolved.append(
                        {
                            "page": page,
                            "proposal": item,
                            "existing_description": metadata.get("description", ""),
                            "windows": context.windows,
                            "diagnostic": (
                                "Read a narrower relevant range or resume the exact "
                                "selection before updating."
                            )
                            if truncated
                            else "Relevant current originals have not been located.",
                        }
                    )
        if unresolved:
            return (
                "Confirm native reading scope and current-source editing intent "
                "for unresolved proposals. "
                "Return a corrected complete concepts/entities plan. Supply relevant native "
                "reading_locations for necessary updates, or withdraw unnecessary proposals "
                "by omitting them or choosing related for pure navigation. Keep all necessary "
                "new knowledge and valid proposals. Describe the proposed increment in brief. "
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

    def _source(self, path: str):
        if path not in self._parsed:
            data = self._read(path)
            raw = json.loads(data) if path.endswith(".json") else data.decode("utf-8")
            if path.endswith(".content.json") and not isinstance(raw, dict):
                raise ValueError("Native text source requires a JSON object")
            self._parsed[path] = raw
        return self._parsed[path]

    @property
    def original_path(self) -> str:
        path = self._path()
        if path is None:
            raise PageResponseError(
                "document_summary_original_unread", "Current original is missing"
            )
        return path

    def original_identity(self) -> dict:
        path = self._path()
        return (
            {"path": path, "sha256": hashlib.sha256(self._read(path)).hexdigest()} if path else {}
        )

    def _document_selection(self, path: str) -> tuple[dict, dict]:
        if path in self._selections:
            return self._selections[path]
        raw = self._source(path)
        if path.endswith(".content.json"):
            from openkb.block_package import read_block_selection
            from openkb.text_source import read_text_selection

            selected = read_block_selection(raw) if raw.get("blocks") else read_text_selection(raw)
            location = (
                {"blocks": ",".join(map(str, selected["block_range"]))}
                if raw.get("blocks")
                else {"chars": f"0:{len(raw['text'])}"}
            )
        elif path.endswith(".json"):
            from openkb.source_pages import read_page_selection

            selected = read_page_selection(raw)
            location = {"pages": ",".join(map(str, selected["page_range"]))}
        else:
            selected = {"content": raw, "source_spans": [[0, len(raw)]]}
            location = {"chars": f"0:{len(raw)}"}
        self._selections[path] = selected, location
        return selected, location

    def iter_document_windows(self, *, fits: Callable[[dict], bool]) -> Iterator[dict]:
        """Read the entire fixed document using actual reader selection offsets."""
        from openkb.agent.source_window_ranges import iter_document_windows

        yield from iter_document_windows(self, fits=fits)

    def split_document_window(self, window: dict) -> list[dict]:
        from openkb.agent.source_window_ranges import split_document_window

        identity = self.original_identity()
        if any(window.get(key) != value for key, value in identity.items()):
            raise PageResponseError("document_summary_reader_invalid", "Split source changed")
        selected, _ = self._document_selection(self.original_path)
        return split_document_window(selected, window)

    def _originals(self, page: str) -> list[str]:
        pending, seen, sources = [page], set(), []
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            if path.startswith("sources/") and path.endswith((".md", ".json")):
                # A normalization and its native map represent one original.
                name = (
                    path.removeprefix("sources/")
                    .removesuffix(".content.json")
                    .removesuffix(".json")
                    .removesuffix(".md")
                )
                sources.append(self._path(name) or path)
                continue
            try:
                metadata = frontmatter.parse(self._read(path).decode("utf-8"))
            except FileNotFoundError:
                # Pending summaries can be excluded while a newer page still
                # links to them. Keep the unresolved association visible.
                sources.append(path)
                continue
            links = metadata.get("sources", [])
            if not isinstance(links, list) or any(not isinstance(n, str) for n in links):
                raise ValueError("Invalid compile source associations")
            pending.extend(links)
            if pointer := metadata.get("full_text"):
                if not isinstance(pointer, str):
                    raise ValueError("Invalid summary original pointer")
                pending.append(pointer)
        return sorted(set(sources))

    def _path(self, doc_name: str | None = None) -> str | None:
        name = self.doc_name if doc_name is None else doc_name
        for suffix in (".content.json", ".json", ".md"):
            path = f"sources/{name}{suffix}"
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
                    raw = self._source(source_path)
                    self._searches[source_path] = SourceSearch(raw)
                hits = search_units(self._searches[source_path], terms)
                locations = [hit["location"] for hit in hits[:MAX_EVIDENCE_UNITS]]
                if locations:
                    for key in ("pages", "blocks"):
                        if key in locations[0]:
                            from openkb.agent.source_window_ranges import (
                                expand_navigation_locations,
                            )

                            return {key: expand_navigation_locations(locations, key, navigation)}
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
            # A node's own span can end at its first child heading. Selecting
            # the named section also selects the descendants' native spans.
            for node in navigation_nodes([nodes[identity] for identity in requested]):
                start, end = node.get("start_index"), node.get("end_index")
                if type(start) is not int or type(end) is not int or not 1 <= start <= end:
                    raise PageResponseError("page_invalid_evidence", "Invalid navigation range")
                numbers.update(range(start, end + 1))
            source_path = path or self._path() or ""
            key = "blocks" if source_path.endswith(".content.json") else "pages"
            return {key: ",".join(map(str, sorted(numbers)))}
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

            raw = self._source(path)
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
            projected = project_evidence(
                selected, offset=location.get("offset", 0), budget=remaining, allow_partial=True
            )
            result.update(projected)
            result["native_location"] = location or {"chars": f"0:{len(raw['text'])}"}
        elif path.endswith(".json"):
            from openkb.agent.evidence_payload import project_evidence
            from openkb.source_pages import read_page_selection

            raw = self._source(path)
            if set(location) - {"pages", "offset"}:
                raise PageResponseError("page_invalid_evidence", "Use this source's physical pages")
            if "pages" not in location:
                return {**result, "diagnostics": ["No relevant original pages located"]}
            selected = read_page_selection(raw, location["pages"])
            if selected["diagnostics"]:
                raise PageResponseError("page_invalid_evidence", str(selected["diagnostics"]))
            projected = project_evidence(
                selected,
                offset=location.get("offset", 0),
                fits=lambda payload: len(payload["content"]) <= remaining,
                allow_partial=True,
            )
            result.update(projected)
            result.update(
                native_location=location,
                images=[
                    image for unit in projected.get("units", []) for image in unit.get("images", [])
                ],
                range_complete=projected.get("continuation") is None and bool(projected["content"]),
                coverage="complete"
                if selected["page_range"] == [p["page"] for p in raw]
                and projected["coverage"] == "complete"
                else "partial",
            )
        else:
            text = data.decode("utf-8")
            start, end = 0, len(text)
            if "chars" in location:
                start, end = map(int, location["chars"].split(":"))
                if not 0 <= start < end <= len(text):
                    raise PageResponseError("page_invalid_evidence", "Invalid original text range")
            from openkb.agent.evidence_payload import project_evidence

            selected = {"content": text[start:end], "source_spans": [[start, end]]}
            projected = project_evidence(
                selected,
                offset=location.get("offset", 0),
                fits=lambda payload: len(payload["content"]) <= remaining,
                allow_partial=True,
            )
            result.update(projected)
            result.update(
                native_location={"chars": f"{start}:{end}", **location},
                range_complete=projected.get("continuation") is None and bool(projected["content"]),
                coverage="complete" if projected["content"] == text else "partial",
            )
        result.update(
            selection_offset=location.get("offset", 0), selection_length=len(selected["content"])
        )
        return render_original_window(result, selected)

    def read_selection(self, path: str, location: dict, *, budget: int | None = None) -> list[dict]:
        """Consume one necessary native selection with exact, reader-owned continuation."""
        return list(self.iter_selection(path, location, budget=budget))

    def iter_selection(
        self, path: str, location: dict, *, budget: int | None = None, allow_suffix: bool = False
    ) -> Iterator[dict]:
        """Project successive windows without eagerly materializing unread suffixes."""
        from openkb.llm_execution import check_model_stop

        if location.get("offset", 0) != 0 and not allow_suffix:
            raise PageResponseError("page_invalid_continuation", "A selection must start at zero")
        budget = MAX_EVIDENCE_CHARS if budget is None else budget
        offset = location.get("offset", 0)
        while True:
            check_model_stop()
            window = self._window(path, {**location, "offset": offset}, budget)
            end = offset + len(window["content"])
            continuation = window.get("continuation")
            length = window.get("selection_length")
            if not window["content"]:
                if offset:
                    raise PageResponseError("page_invalid_continuation", "Reader made no progress")
            elif continuation is None:
                if end != length:
                    raise PageResponseError("page_invalid_continuation", "Selection tail unread")
            elif (
                type(length) is not int
                or type(continuation) is not int
                or not offset < continuation == end < length
            ):
                raise PageResponseError("page_invalid_continuation", "Noncontiguous selection")
            yield window
            if not window["content"] or continuation is None:
                return
            offset = continuation

    def prepare(
        self, item: dict, previous_page: str | None = None, *, long: bool = False
    ) -> PageOriginalContext:
        if previous_page and (self.wiki / previous_page).is_file():
            metadata = frontmatter.parse(self._read(previous_page).decode("utf-8"))
            aliases = metadata.get("aliases", [])
            if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
                raise ValueError("Existing page aliases must be a list of names")
            item = {**item, "aliases": aliases}
        paths: dict[str, dict] = {}
        if long and (path := self._path()):
            paths[path] = self._locations(item)
        if previous_page and (self.wiki / previous_page).is_file():
            for path in self._originals(previous_page):
                if not long and path == self._path():
                    continue  # The dedicated short-original message already contains it in full.
                location = {}
                if (
                    path.endswith(".json")
                    and not path.endswith(".content.json")
                    and self.kb_dir
                    and (self.wiki / path).is_file()
                ):
                    from openkb.source_navigation import published_navigation

                    previous = published_navigation(self.kb_dir, self.view_id, Path(path).stem)
                    location = self._locations(
                        {
                            "name": item["name"],
                            "title": item.get("title", ""),
                            "aliases": item.get("aliases", []),
                        },
                        path=path,
                        navigation=previous.get("structure", []),
                    )
                paths.setdefault(path, location)
        windows: list[dict] = []
        remaining = MAX_EVIDENCE_CHARS
        for path, location in paths.items():
            try:
                self._read(path)
                self.figure_source(path)
                selected_windows = (
                    list(self.iter_selection(path, location, allow_suffix=previous_page is None))
                    if previous_page or long
                    else [self._window(path, location, remaining)]
                    if remaining
                    else [{"path": path, "coverage": "unread", "content": ""}]
                )
            except FileNotFoundError as error:
                if error.filename != str((self.wiki / path).resolve()):
                    raise
                selected_windows = [
                    {
                        "path": path,
                        "coverage": "unread",
                        "content": "",
                        "content_kind": "unavailable_source_association",
                        "diagnostics": [
                            "Linked source association unavailable; do not infer absence"
                        ],
                    }
                ]
            for window in selected_windows:
                if not window["content"].strip():
                    window["coverage"] = "unread"
                remaining = max(0, remaining - len(window["content"]))
                window["source_role"] = "current" if path == self._path() else "prior_linked_source"
                window["unread"] = window["coverage"] != "complete"
                window["reading_action"] = {
                    "source_path": path,
                    "native_location": location,
                    "continuation": (
                        {**location, "offset": window["continuation"]}
                        if type(window.get("continuation")) is int
                        else window.get("continuation")
                    ),
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
                    "Current short-document original is supplied in full in its dedicated "
                    "original message. The windows below supplement it with linked sources.\n"
                    if not long
                    else ""
                )
                + "Original source windows (bounded, frozen):\n"
                + json.dumps(windows, ensure_ascii=False)
                + "\nPrior page basis (retain these source revisions and any pending refresh): "
                + json.dumps(prior_evidence, ensure_ascii=False)
                + "\nReader window limits are separate from total request capacity. "
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
        return PageOriginalContext(
            {"role": "user", "content": blocks},
            any(w["content"].strip() for w in windows),
            not long or any(w["path"] == self._path() and w["content"].strip() for w in windows),
            tuple(windows),
        )
