"""Validated generation ranges and deterministic, versioned page figure galleries."""

import json
from dataclasses import dataclass
from pathlib import Path

from openkb.agent.page_response import PageResponseError
from openkb.compile_checkpoints import digest
from openkb.figure_assets import FigureOccurrence, relative_figure, verify_figure
from openkb.markdown_images import apply_image_edits, image_references

START = "<!-- openkb-figures:start -->"
END = "<!-- openkb-figures:end -->"


def split_gallery(content: str) -> tuple[str, list[dict]]:
    """Only consume the application's explicitly delimited figure records."""
    if START not in content:
        return content, []
    before, _, tail = content.partition(START)
    gallery, marker, after = tail.partition(END)
    if not marker or START in after:
        raise PageResponseError("figure_invalid_gallery", "Invalid saved figure gallery")
    lines = gallery.strip().splitlines()
    if not lines:
        raise PageResponseError("figure_invalid_gallery", "Empty saved figure gallery")
    line = lines[0]
    if not line.startswith("<!-- records: ") or not line.endswith(" -->"):
        raise PageResponseError("figure_invalid_gallery", "Missing saved occurrence records")
    try:
        entries = json.loads(line[len("<!-- records: ") : -len(" -->")])
        if not isinstance(entries, list):
            raise ValueError("Expected records")
        for entry in entries:
            if set(entry) != {"figure", "historical"} or type(entry["historical"]) is not bool:
                raise ValueError("Invalid gallery record")
            FigureOccurrence.model_validate(entry["figure"])
    except (ValueError, TypeError) as error:
        raise PageResponseError("figure_invalid_gallery", str(error)) from error
    return before.rstrip() + after, entries


def render_gallery(wiki: Path, page: str, body: str, entries: list[dict]) -> str:
    if not entries:
        return body
    records = (
        json.dumps(entries, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
    )
    lines = [body.rstrip(), "", START, "<!-- records: " + records + " -->"]
    for historical in (False, True):
        selected = [e for e in entries if e["historical"] is historical]
        if not selected:
            continue
        lines.extend(["", "## 历史参考原图" if historical else "## 本节原图", ""])
        lines.append(
            "保留自此前页面版本，未确认仍对应当前正文。"
            if historical
            else "原图已保留，未进行图片理解。位置关联不表示图意已经核实。"
        )
        for entry in selected:
            figure = FigureOccurrence.model_validate(entry["figure"])
            verify_figure(wiki, figure)
            location = (
                f"第 {figure.location['page']} 页"
                if "page" in figure.location
                else f"字符 {figure.location['start']}:{figure.location['end']}"
            )
            caption = (
                figure.caption
                if figure.caption.casefold() not in {"", "image", "original"}
                else f"原图，{location}"
            )
            caption = (
                caption.replace("\\", "\\\\")
                .replace("[", "\\[")
                .replace("]", "\\]")
                .replace("\n", " ")
            )
            provenance = (
                f"source_revision_id: `{figure.source_revision_id}`；"
                f"processing_revision_id: `{figure.processing_revision_id}`"
                if figure.source_revision_id and figure.processing_revision_id
                else f"来源版本 ID 未记录；source_sha256: `{figure.source_digest}`"
            )
            lines.extend(
                [
                    "",
                    f"![{caption}]({relative_figure(wiki, page, figure.asset_path)})",
                    f"出处：`{figure.source_path}`，{location}；{provenance}。",
                ]
            )
    return "\n".join([*lines, END, ""])


def validate_ranges(value: dict, ranges: tuple[str, ...]) -> list[str]:
    selected = value.get("source_ranges")
    if (
        not isinstance(selected, list)
        or not selected
        or any(not isinstance(r, str) or r not in ranges for r in selected)
        or len(selected) != len(set(selected))
    ):
        raise PageResponseError(
            "page_invalid_source_ranges",
            "source_ranges must be a nonempty JSON array of unique ID strings, not objects. "
            "Select only supplied frozen range IDs: " + json.dumps(ranges),
        )
    return selected


@dataclass(frozen=True)
class PageFigures:
    wiki: Path
    ranges: tuple[dict, ...]
    figures: tuple[FigureOccurrence, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(r["id"] for r in self.ranges)

    def message(self) -> dict:
        return {
            "role": "user",
            "content": "Figure source ranges:\n"
            + json.dumps(
                {
                    "protocol": "page-source-ranges-v2",
                    "ranges": self.ranges,
                    "source_ranges_schema": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"type": "string", "enum": self.ids},
                    },
                    "instruction": (
                        "Return source_ranges alongside description/content or block edits, "
                        "selecting supplied IDs used by the FINAL body (only changed content "
                        "for updates). The full original remains available. A range can have "
                        "no images. These locators bind figures, not fact certification. "
                        "unchanged has no ranges. Never invent image paths; the application "
                        "renders original figures."
                    ),
                },
                ensure_ascii=False,
            ),
        }

    def decorate(
        self, page: str, value: dict, *, previous: str = "", replaced: bool = False
    ) -> dict:
        chosen = (
            validate_ranges(value, self.ids) if self.figures else value.get("source_ranges", [])
        )
        selected_ids = {f for r in self.ranges if r["id"] in chosen for f in r["figures"]}
        current = [f for f in self.figures if f.occurrence_id in selected_ids]
        body, _ = split_gallery(value["content"])
        _, old = split_gallery(previous)
        old_body, _ = split_gallery(previous)
        surviving_refs = {r.source for r in image_references(old_body)} & {
            r.source for r in image_references(body)
        }
        entries = {}
        for entry in old:
            figure = FigureOccurrence.model_validate(entry["figure"])
            still_bound = relative_figure(self.wiki, page, figure.asset_path) in surviving_refs
            entries[figure.occurrence_id] = {
                **entry,
                "historical": entry["historical"] or (replaced and not still_bound),
            }
        for figure in current:
            entries[figure.occurrence_id] = {
                "figure": figure.model_dump(mode="json"),
                "historical": False,
            }
        # Generated paths must match a known occurrence in this page's declared range.
        known = [FigureOccurrence.model_validate(e["figure"]) for e in entries.values()]
        edits = []
        previous_refs = {r.source for r in image_references(old_body)}
        for ref in image_references(body):
            if ref.source in previous_refs:
                continue
            matches = [
                f
                for f in known
                if ref.local_path
                in {
                    f.original_path,
                    f.original_path.removeprefix("sources/"),
                    f.asset_path,
                    str(Path(relative_figure(self.wiki, page, f.asset_path))),
                }
            ]
            if not matches:
                raise PageResponseError(
                    "figure_invalid_reference", f"Unverified generated image: {ref.source}"
                )
            if len({f.asset_digest for f in matches}) != 1:
                raise PageResponseError(
                    "figure_ambiguous_reference", f"Ambiguous generated image: {ref.source}"
                )
            edits.append(
                (
                    ref.start,
                    ref.end,
                    ref.relocated(relative_figure(self.wiki, page, matches[0].asset_path)),
                )
            )
        body = apply_image_edits(body, edits)
        # A generated inline reference already has the same content identity; the gallery
        # supplies its provenance. Render once by removing duplicate generated markup.
        for ref in reversed(image_references(body)):
            if (
                ref.source in {relative_figure(self.wiki, page, f.asset_path) for f in current}
                and not ref.html
            ):
                body = body[: ref.start] + body[ref.end :]
        return {**value, "content": render_gallery(self.wiki, page, body, list(entries.values()))}


def source_ranges(
    source_path: str, raw, figures: list[FigureOccurrence], identity: dict
) -> tuple[dict, ...]:
    """Expose native pages or heading-bounded character ranges without clipping text."""
    from openkb.agent.document_summary import summary_sections
    from openkb.figure_assets import validate_figure_source

    validate_figure_source(raw)
    if isinstance(raw, list):
        parts = [(p.get("content", ""), {"page": p["page"]}) for p in raw]
    else:
        text = raw.get("text", "") if isinstance(raw, dict) else raw
        parts = [
            (text[s["start"] : s["end"]], {"start": s["start"], "end": s["end"]})
            for s in summary_sections(text)
        ]
    ranges: list[dict] = []
    for text, location in parts:
        basis = {"source_path": source_path, **identity, "location": location}
        selected = [
            f.occurrence_id
            for f in figures
            if (
                f.location.get("page") == location["page"]
                if "page" in location
                else location["start"] <= f.location["start"] < location["end"]
            )
        ]
        ranges.append(
            {
                "id": digest(basis),
                **basis,
                "label": text.strip().split("\n")[0][:160],
                "figures": selected,
            }
        )
    return tuple(ranges)


def page_figure_context(sources, windows, *, long: bool) -> PageFigures:
    ranges: list[dict] = []
    figures = {}
    if not long and (path := sources._path()):
        identity, entries = sources.figure_source(path)
        ranges.extend(source_ranges(path, sources._source(path), entries, identity))
        figures.update({f.occurrence_id: f for f in entries})
    for window in windows:
        if not window.get("content", "").strip():
            continue
        path = window["path"]
        identity, entries = sources.figure_source(path)
        location = {
            k: window[k]
            for k in ("source_spans", "page_spans", "selection_offset", "native_location")
            if k in window
        }
        basis = {"source_path": path, **identity, "location": location}
        pages = {p["page"] for p in window.get("page_spans", [])}
        projected_images = (
            {
                (unit["page"], image["path"])
                for unit in window["units"]
                for image in unit.get("images", [])
            }
            if "units" in window
            else None
        )
        selected = [
            f
            for f in entries
            if (
                f.location["page"] in pages
                and (
                    projected_images is None
                    or (f.location["page"], f.original_path) in projected_images
                )
                and (
                    f.reference_span is None
                    or any(
                        span["page"] == f.location["page"]
                        and span["start"]
                        <= f.reference_span[0]
                        < f.reference_span[1]
                        <= span["end"]
                        for span in window.get("page_spans", [])
                    )
                )
                if "page" in f.location
                else any(a <= f.location["start"] < b for a, b in window.get("source_spans", []))
            )
        ]
        figures.update({f.occurrence_id: f for f in selected})
        ranges.append(
            {
                "id": digest(basis),
                **basis,
                "label": window["content"][:160],
                "figures": [f.occurrence_id for f in selected],
            }
        )
    return PageFigures(
        sources.wiki, tuple({r["id"]: r for r in ranges}.values()), tuple(figures.values())
    )
