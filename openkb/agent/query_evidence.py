"""Attach independent, pinned read tools to query, chat and skill agents."""

import json
from dataclasses import replace
from functools import wraps
from typing import Literal

from agents import ToolOutputImage, ToolOutputText, function_tool

from openkb.agent.answer_evidence import ANSWER_EVIDENCE_RULES
from openkb.agent.evidence_session import EvidenceSession
from openkb.agent.tools import get_wiki_page_content, read_wiki_image
from openkb.application.query_views import (
    QuerySelection,
    QueryView,
    read_query_page,
)
from openkb.state import HashRegistry


def evidence_input(input_data):
    """Retain user intent across turns; reacquire factual evidence in this selection."""
    if isinstance(input_data, str):
        return input_data
    return [item for item in input_data if item.get("role") == "user"]


def selection_catalog(selection: QuerySelection) -> str:
    lines = [*selection.missing]
    for view in selection.views:
        lines.append(view.provenance)
        lines.append(
            f"  verified_applicable_versions={json.dumps(view.applicable_versions)}; "
            "filename versions are unverified hints"
        )
        lines.extend(f"  {name}" for name in sorted(view.files) if name.endswith(".md"))
        for name in sorted(view.files):
            if name.startswith("sources/") and name.endswith(".json"):
                from pathlib import Path

                pages = json.loads((view.scope.wiki_dir / name).read_text("utf-8"))
                if name.endswith(".content.json"):
                    from openkb.text_source import read_text_selection

                    text = read_text_selection(pages)
                    doc_name = Path(name).name.removesuffix(".content.json")
                    if text["block_count"] is not None:
                        lines.append(
                            f"  Content blocks: doc_name={doc_name}; "
                            f"blocks=1-{text['block_count']}; read with get_block_content."
                        )
                    lines.append(
                        f"  Source: doc_name={doc_name}; "
                        f"characters=0:{text['characters']}; tokens={text['tokens']}; "
                        f"unit_kind={text['unit_kind']}; pages=none; "
                        "use get_text_content with chars=START:END"
                    )
                    continue
                if not isinstance(pages, list):
                    raise ValueError("Indexed evidence must contain a page list")
                from openkb.source_pages import read_page_selection

                selected = read_page_selection(pages)
                numbers = selected["page_range"]
                available = f"{min(numbers)}-{max(numbers)}" if numbers else "none"
                lines.append(
                    f"  Source: doc_name={Path(name).stem}; pages={available}; "
                    f"unit_kind={selected['unit_kind'] or 'unknown'}; "
                    f"coverage={selected['coverage']}"
                )
    return "\n".join(lines) or "No permitted knowledge evidence is available."


def _selected(selection: QuerySelection, view_id: str) -> QueryView:
    if not view_id and len(selection.views) == 1:
        return selection.views[0]
    view = next((item for item in selection.views if item.view_id == view_id), None)
    if view is None:
        raise ValueError("Choose one of the allowed evidence views from the catalog")
    return view


def _available(view: QueryView, path: str) -> bool:
    target = (view.scope.wiki_dir / path).resolve()
    return (
        target.is_relative_to(view.scope.wiki_dir)
        and path in view.files
        and target.is_file()
        and HashRegistry.hash_file(target) == view.files[path]
    )


def restrict_query_agent(
    agent, selection: QuerySelection, *, session: EvidenceSession | None = None
):
    """Replace every wiki reader; preserving artifact writers grants no extra knowledge reads."""
    session = session or EvidenceSession(selection)

    def bounded(fn):
        @wraps(fn)
        def invoke(*args, **kwargs):
            session.consume_tool()
            return session.budget.tool_output(fn(*args, **kwargs))

        return invoke

    def render_selection(view, path, locator, selected, offset=0):
        from openkb.agent.evidence_payload import project_evidence

        projected = project_evidence(selected, offset=offset)
        locator += f" offset={offset}" if offset else ""
        revision = view.source_revisions_by_path.get(path)
        read = session.register_read(
            view, path, locator, projected["content"], spans=projected.get("source_spans", ())
        )
        projected["read_id"] = read.read_id
        if revision:
            view = replace(view, source_revision_ids=(revision,))
        return (
            view.provenance
            + "\n\n"
            + json.dumps(projected, ensure_ascii=False)
            + f"\nEvidence locator: {locator}"
        )

    @function_tool
    @bounded
    def read_file(path: str, view_id: str = "", offset: int = 0) -> str:
        """Read a permitted Markdown page in one named evidence view.

        Read index.md without a view_id to see all allowed views and their pages.
        Every other read must select a view_id when multiple versions are available.
        """
        if path == "index.md" and not view_id:
            return selection_catalog(selection)
        view = _selected(selection, view_id)
        if path == "index.md":
            return selection_catalog(QuerySelection(selection.kb_dir, (view,)))
        if not path.endswith(".md"):
            return (
                "Use get_page_content/get_block_content/get_text_content/get_cell_content "
                "for original data."
            )
        content = read_query_page(selection, path, view_id=view.view_id)
        if not _available(view, path):
            return content
        if len(content) > 12000 or offset:
            from openkb.agent.evidence_payload import project_evidence

            # Generated navigation is paginated too; it is never original evidence.
            body = (view.scope.wiki_dir / path).read_text("utf-8")
            projected = project_evidence({"content": body}, offset=offset, budget=12000)
            if path.startswith("sources/"):
                locator = path + (f" offset={offset}" if offset else "")
                read = session.register_read(
                    view,
                    path,
                    locator,
                    projected["content"],
                    spans=projected.get("source_spans", ()),
                )
                projected["read_id"] = read.read_id
            return view.provenance + "\n\n" + json.dumps(projected, ensure_ascii=False)
        if path.startswith("sources/") and _available(view, path):
            body = (view.scope.wiki_dir / path).read_text("utf-8")
            read = session.register_read(view, path, path, body, spans=((0, len(body)),))
            content += f"\nRead ID: {read.read_id}\nEvidence locator: {path}"
        return content

    @function_tool
    @bounded
    def search_originals(terms: list[str], view_id: str = "", doc_name: str = "") -> str:
        """Find original pages/blocks by 1-6 literal subject/configuration terms.

        Choose narrow terms from the question's subject and requested facts.
        Read matching originals next;
        a search miss never proves a field, API, procedure or image is absent.
        """
        from openkb.agent.evidence_search import search_originals as search

        if view_id:
            _selected(selection, view_id)
        return json.dumps(
            search(selection, terms, view_id=view_id, doc_name=doc_name), ensure_ascii=False
        )

    @function_tool
    @bounded
    def get_page_content(
        doc_name: str,
        pages: str,
        view_id: str = "",
        part: Literal["body", "notes"] | None = None,
    ) -> str | list[ToolOutputText | ToolOutputImage]:
        """Read original physical pages in one allowed view.

        Use part=null for PDF or the full physical page. Only slides with a verified
        partition accept part="body" or part="notes". Never pass the string "empty".
        """
        view = _selected(selection, view_id)
        if not _available(view, f"sources/{doc_name}.json"):
            return "No permitted indexed source in this evidence view."
        content = (
            view.provenance
            + "\n\n"
            + get_wiki_page_content(doc_name, pages, str(view.scope.wiki_dir), part)
        )
        if len(content) > 48000:
            return json.dumps(
                {
                    "coverage": "budget_exceeded",
                    "content": "",
                    "diagnostics": [
                        "Requested pages exceed the reading budget; select fewer physical pages."
                    ],
                }
            )
        locator = f"sources/{doc_name}.json pages={pages} part={part}"
        from openkb.source_pages import read_page_selection, select_page_part

        selected = select_page_part(
            read_page_selection(
                json.loads((view.scope.wiki_dir / f"sources/{doc_name}.json").read_text("utf-8")),
                pages,
            ),
            part,
        )
        read = session.register_read(view, f"sources/{doc_name}.json", locator, selected["content"])
        content += f"\nRead ID: {read.read_id}"
        paths = list(
            dict.fromkeys(
                image["path"] for unit in selected["units"] for image in unit.get("images", [])
            )
        )
        outputs: list[ToolOutputText | ToolOutputImage] = []
        # Attach a bounded set from the exact requested pages. Remaining references
        # stay explicit, so a later get_image can complete a visual investigation.
        for path in paths[:4]:
            if _available(view, path) and (view.scope.wiki_dir / path).stat().st_size <= 5_000_000:
                result = read_wiki_image(path, str(view.scope.wiki_dir))
                if result["type"] == "image":
                    visual = session.register_read(
                        view,
                        path,
                        locator + " image=" + path,
                        "",
                        kind="image",
                        image_url=result["image_url"],
                    )
                    outputs.extend(
                        [
                            ToolOutputText(
                                text=f"Original page image: {path}; Read ID: {visual.read_id}"
                            ),
                            ToolOutputImage(image_url=result["image_url"]),
                        ]
                    )
        content += f"\nEvidence locator: {locator}"
        if paths:
            content += "\nInspect attached images before concluding information is missing. "
            content += "Use get_image for relevant images not attached: " + ", ".join(paths)
        return [ToolOutputText(text=content), *outputs] if outputs else content

    @function_tool
    @bounded
    def get_text_content(doc_name: str, chars: str, view_id: str = "", offset: int = 0) -> str:
        """Read a frozen Unicode codepoint range START:END (0-based, end exclusive).

        Returns exact original-file locators separately from normalized text coordinates.
        Text has no physical pages. Choose doc_name and view_id from the evidence catalog.
        """
        import json

        from openkb.text_source import read_text_selection

        view = _selected(selection, view_id)
        path = f"sources/{doc_name}.content.json"
        if not _available(view, path):
            return "No permitted frozen text in this evidence view."
        selected = read_text_selection(
            json.loads((view.scope.wiki_dir / path).read_text("utf-8")), chars
        )
        locator = f"{path} chars={chars}"
        return render_selection(view, path, locator, selected, offset)

    @function_tool
    @bounded
    def get_block_content(doc_name: str, blocks: str, view_id: str = "", offset: int = 0) -> str:
        """Read frozen blocks, original ranges and display context in one allowed view.

        blocks uses one-based ordinals (e.g. 1,3-5). These are not physical pages.
        """
        import json

        from openkb.block_package import read_block_selection

        view = _selected(selection, view_id)
        path = f"sources/{doc_name}.content.json"
        if not _available(view, path):
            return "No permitted content blocks in this evidence view."
        selected = read_block_selection(
            json.loads((view.scope.wiki_dir / path).read_text("utf-8")), blocks
        )
        locator = f"{path} blocks={blocks}"
        return render_selection(view, path, locator, selected, offset)

    @function_tool
    @bounded
    def get_cell_content(doc_name: str, cells: str, view_id: str = "", offset: int = 0) -> str:
        """Read finite original worksheet ranges in comma-separated A1 notation.

        Prefer this for sheet questions. Preserve formulas, cache status, merges
        and hidden cells. For any paginated read repeat the SAME selection with
        offset=continuation; coverage describes only this response.
        """
        from openkb.text_source import read_text_selection
        from openkb.workbooks.selection import select_cells

        view = _selected(selection, view_id)
        path = f"sources/{doc_name}.content.json"
        if not _available(view, path):
            return "No permitted worksheet in this evidence view."
        # select_cells uses absolute normalized coordinates: never pass sliced text.
        full = read_text_selection(json.loads((view.scope.wiki_dir / path).read_text("utf-8")))
        selected = select_cells(full, cells)
        cursor, segments = 0, []
        for origin in selected["origin_locators"]:
            a, b = origin["normalized_span"]
            prefix = origin["sheet_cell"]["cell"]["coordinate"] + ": "
            segments.append((cursor + len(prefix), cursor + len(prefix) + b - a, a, b))
            cursor += len(prefix) + b - a + 2
        selected["_content_segments"] = segments
        return render_selection(view, path, f"{path} cells={cells}", selected, offset)

    @function_tool
    @bounded
    def get_image(
        image_path: str, view_id: str = ""
    ) -> list[ToolOutputText | ToolOutputImage] | ToolOutputText:
        """View a retained image belonging to one permitted evidence view."""
        view = _selected(selection, view_id)
        path = image_path if _available(view, image_path) else f"sources/{image_path}"
        if not _available(view, path):
            return ToolOutputText(text="No permitted image in this evidence view.")
        result = read_wiki_image(path, str(view.scope.wiki_dir))
        if result["type"] == "image":
            read = session.register_read(
                view, path, path, "", kind="image", image_url=result["image_url"]
            )
            return [
                ToolOutputText(text=f"Original image: {path}; Read ID: {read.read_id}"),
                ToolOutputImage(image_url=result["image_url"]),
            ]
        return ToolOutputText(text=result["text"])

    @function_tool
    @bounded
    def record_source_fact(
        subject: str,
        setting: str,
        condition: str,
        quote: str,
        locator: str,
        view_id: str = "",
        slot_id: str = "",
    ) -> str:
        """Record one rule before combining sources. Keep subject/setting/condition separate.

        quote must be verbatim from an original read, not a summary. locator must
        match that read's evidence locator. Use an empty condition if none is stated.
        Copy subject, setting and condition verbatim from the SAME quote, including
        enough context to identify the subject. Do not borrow another rule's condition.
        Recording a quote verifies its origin, not the interpretation of the rule.
        """
        view = _selected(selection, view_id)
        return session.record_fact(
            view.view_id,
            subject=subject,
            setting=setting,
            condition=condition,
            quote=quote,
            locator=locator,
            slot_id=slot_id,
        )

    readers = {
        "record_source_fact",
        "read_file",
        "get_page_content",
        "get_block_content",
        "get_text_content",
        "get_cell_content",
        "search_originals",
        "get_image",
        "read_wiki_file",
        "list_wiki_dir",
        "query_wiki",
    }
    extra_tools = [
        session.writes.guard(tool)
        for tool in agent.tools
        if getattr(tool, "name", None) not in readers
    ]
    instructions = (
        "\n\n# Permitted version evidence\n"
        "Use only the read tools' permitted evidence for factual knowledge claims. "
        "Answer each version separately and preserve differences; never flatten opposing "
        "rules into one unqualified fact. Cite actual product versions and source revisions. "
        "A requested version with missing evidence has a gap; do not fill it with another "
        "version, an earlier conversation, or an unversioned artifact. Historical references "
        "are explicitly separate and never establish facts for the requested version.\n"
        "Use read_file(index.md) to list evidence. "
        "Older traversal tools are replaced by these readers.\n"
        "Earlier assistant replies and tool outputs are omitted. Use user messages for intent "
        "and read the permitted sources again for every knowledge claim.\n"
        + ANSWER_EVIDENCE_RULES
        + selection_catalog(selection)
    )
    return agent.clone(
        tools=[
            read_file,
            search_originals,
            get_page_content,
            get_text_content,
            get_block_content,
            get_cell_content,
            get_image,
            record_source_fact,
            *extra_tools,
        ],
        instructions=(agent.instructions or "") + instructions,
    )


def evidence_answer(answer: str, selection: QuerySelection) -> str:
    from openkb.agent.answer_finalization import decide_answer

    return decide_answer(answer, selection).answer
