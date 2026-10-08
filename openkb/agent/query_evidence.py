"""Attach independent, pinned read tools to query, chat and skill agents."""

import json
from typing import Literal

from agents import ToolOutputImage, ToolOutputText, function_tool

from openkb.agent.tools import get_wiki_page_content, read_wiki_image
from openkb.application.query_views import (
    QuerySelection,
    QueryView,
    read_query_page,
    selection_current,
)
from openkb.state import HashRegistry


def evidence_input(input_data):
    """Preserve conversation context; source readers remain pinned per request."""
    return input_data


def selection_catalog(selection: QuerySelection) -> str:
    lines = [*selection.missing]
    for view in selection.views:
        lines.append(f"Collection: {view.view_id}")
        if "index.md" in view.files:
            lines.append(read_query_page(selection, "index.md", view_id=view.view_id))
        else:
            lines.extend(f"  {name}" for name in sorted(view.files) if name.endswith(".md"))
        for name in sorted(view.files):
            if name.startswith("sources/") and name.endswith(".json"):
                import json
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


def restrict_query_agent(agent, selection: QuerySelection):
    """Replace every wiki reader; preserving artifact writers grants no extra knowledge reads."""

    @function_tool
    def read_file(path: str, view_id: str = "") -> str:
        """Read a permitted Markdown page in one named evidence view.

        Read index.md without a view_id to see all allowed views and their pages.
        Every other read must select a view_id when multiple versions are available.
        """
        if path == "index.md" and not view_id:
            return selection_catalog(selection)
        view = _selected(selection, view_id)
        if path == "index.md":
            return selection_catalog(QuerySelection(selection.kb_dir, (view,)))
        return read_query_page(selection, path, view_id=view.view_id)

    @function_tool
    def get_page_content(
        doc_name: str, pages: str, view_id: str = "", part: Literal["body", "notes"] | None = None
    ) -> str | list[ToolOutputText | ToolOutputImage]:
        """Read original physical pages from the knowledge base.

        For slides, part may be body or notes; null includes both without extra pages.
        """
        view = _selected(selection, view_id)
        if not _available(view, f"sources/{doc_name}.json"):
            return "No permitted indexed source in this evidence view."
        content = (
            view.provenance
            + "\n\n"
            + get_wiki_page_content(doc_name, pages, str(view.scope.wiki_dir), part or None)
        )
        from openkb.source_pages import read_page_selection, select_page_part

        selected = select_page_part(
            read_page_selection(
                json.loads((view.scope.wiki_dir / f"sources/{doc_name}.json").read_text("utf-8")),
                pages,
            ),
            part or None,
        )
        paths = list(
            dict.fromkeys(
                image["path"] for unit in selected["units"] for image in unit.get("images", [])
            )
        )
        outputs: list[ToolOutputText | ToolOutputImage] = []
        for path in paths[:4]:
            if _available(view, path) and (view.scope.wiki_dir / path).stat().st_size <= 5_000_000:
                image = read_wiki_image(path, str(view.scope.wiki_dir))
                if image["type"] == "image":
                    outputs.extend(
                        [
                            ToolOutputText(text=f"Original page image: {path}"),
                            ToolOutputImage(image_url=image["image_url"]),
                        ]
                    )
        if paths:
            content += "\nUse get_image for relevant images not attached: " + ", ".join(paths)
        return [ToolOutputText(text=content), *outputs] if outputs else content

    @function_tool
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
        from openkb.agent.evidence_payload import project_evidence

        return (
            view.provenance
            + "\n\n"
            + json.dumps(project_evidence(selected, offset=offset), ensure_ascii=False)
        )

    @function_tool
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
        from openkb.agent.evidence_payload import project_evidence

        return (
            view.provenance
            + "\n\n"
            + json.dumps(project_evidence(selected, offset=offset), ensure_ascii=False)
        )

    @function_tool
    def get_cell_content(doc_name: str, cells: str, view_id: str = "", offset: int = 0) -> str:
        """Read finite worksheet ranges in A1 notation, retaining cell locations.

        Repeat the same range with offset=continuation for paginated results.
        """
        from openkb.agent.evidence_payload import project_evidence
        from openkb.text_source import read_text_selection
        from openkb.workbooks.selection import select_cells

        view = _selected(selection, view_id)
        path = f"sources/{doc_name}.content.json"
        if not _available(view, path):
            return "No permitted worksheet in this knowledge base."
        full = read_text_selection(json.loads((view.scope.wiki_dir / path).read_text("utf-8")))
        selected = select_cells(full, cells)
        cursor, segments = 0, []
        for origin in selected["origin_locators"]:
            a, b = origin["normalized_span"]
            prefix = origin["sheet_cell"]["cell"]["coordinate"] + ": "
            segments.append((cursor + len(prefix), cursor + len(prefix) + b - a, a, b))
            cursor += len(prefix) + b - a + 2
        selected["_content_segments"] = segments
        return (
            view.provenance
            + "\n\n"
            + json.dumps(project_evidence(selected, offset=offset), ensure_ascii=False)
        )

    @function_tool
    def search_originals(terms: list[str], view_id: str = "", doc_name: str = "") -> str:
        """Locate phrases in the selected documents; read the matching original next."""
        from openkb.agent.evidence_search import search_originals as search

        if view_id:
            _selected(selection, view_id)
        return json.dumps(
            search(selection, terms, view_id=view_id, doc_name=doc_name), ensure_ascii=False
        )

    @function_tool
    def get_image(image_path: str, view_id: str = "") -> ToolOutputImage | ToolOutputText:
        """View a retained image belonging to one permitted evidence view."""
        view = _selected(selection, view_id)
        path = image_path if _available(view, image_path) else f"sources/{image_path}"
        if not _available(view, path):
            return ToolOutputText(text="No permitted image in this evidence view.")
        result = read_wiki_image(path, str(view.scope.wiki_dir))
        return (
            ToolOutputImage(image_url=result["image_url"])
            if result["type"] == "image"
            else ToolOutputText(text=result["text"])
        )

    readers = {
        "read_file",
        "get_page_content",
        "get_block_content",
        "get_text_content",
        "get_image",
        "get_cell_content",
        "search_originals",
        "record_source_fact",
        "read_wiki_file",
        "list_wiki_dir",
        "query_wiki",
    }
    extra_tools = [tool for tool in agent.tools if getattr(tool, "name", None) not in readers]
    instructions = (
        "\n\n# Reading this knowledge base\n"
        "Use read_file(index.md) for the document and concept index. "
        "For text sources use get_text_content/get_block_content; for worksheets "
        "use get_cell_content. These preserve original locations. "
        "Distinguish conflicting statements using the documents themselves and cite them."
    )
    return agent.clone(
        tools=[
            read_file,
            get_page_content,
            get_text_content,
            get_block_content,
            get_cell_content,
            search_originals,
            get_image,
            *extra_tools,
        ],
        instructions=(agent.instructions or "") + instructions,
    )


def evidence_answer(answer: str, selection: QuerySelection) -> str:
    """Keep the generated answer; source metadata does not rewrite its claims."""
    if not selection.has_evidence:
        return "知识库中没有可读取的原文，请先导入资料。"
    if not selection_current(selection):
        return answer.rstrip() + "\n\n回答期间资料发生变更，请重新提问以读取当前内容。"
    return answer
