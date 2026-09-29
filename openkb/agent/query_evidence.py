"""Attach independent, pinned read tools to query, chat and skill agents."""

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
    """Retain user intent across turns; reacquire factual evidence in this selection."""
    if isinstance(input_data, str):
        return input_data
    return [item for item in input_data if item.get("role") == "user"]


def selection_catalog(selection: QuerySelection) -> str:
    lines = [*selection.missing]
    for view in selection.views:
        lines.append(view.provenance)
        lines.extend(f"  {name}" for name in sorted(view.files) if name.endswith(".md"))
        for name in sorted(view.files):
            if name.startswith("sources/") and name.endswith(".json"):
                import json
                from pathlib import Path

                pages = json.loads((view.scope.wiki_dir / name).read_text("utf-8"))
                if name.endswith(".content.json"):
                    from openkb.text_source import read_text_selection

                    text = read_text_selection(pages)
                    lines.append(
                        f"  Source: doc_name={Path(name).name.removesuffix('.content.json')}; "
                        f"characters=0:{text['characters']}; tokens={text['tokens']}; "
                        "unit_kind=text; pages=none; use get_text_content with chars=START:END"
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
    def get_page_content(doc_name: str, pages: str, view_id: str = "") -> str:
        """Read original indexed pages within one allowed evidence view."""
        view = _selected(selection, view_id)
        if not _available(view, f"sources/{doc_name}.json"):
            return "No permitted indexed source in this evidence view."
        return (
            view.provenance
            + "\n\n"
            + get_wiki_page_content(doc_name, pages, str(view.scope.wiki_dir))
        )

    @function_tool
    def get_text_content(doc_name: str, chars: str, view_id: str = "") -> str:
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
        return view.provenance + "\n\n" + json.dumps(selected, ensure_ascii=False)

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
        "get_text_content",
        "get_image",
        "read_wiki_file",
        "list_wiki_dir",
        "query_wiki",
    }
    extra_tools = [tool for tool in agent.tools if getattr(tool, "name", None) not in readers]
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
        + selection_catalog(selection)
    )
    return agent.clone(
        tools=[read_file, get_page_content, get_text_content, get_image, *extra_tools],
        instructions=(agent.instructions or "") + instructions,
    )


def evidence_answer(answer: str, selection: QuerySelection) -> str:
    """Expose the actual immutable revisions even if a model omits its scope labels."""
    details = [view.provenance for view in selection.views]
    details.extend(selection.missing)
    if not selection_current(selection):
        details.insert(
            0,
            "Evidence changed during this answer. Treat these revisions as historical; "
            "ask again for current verification.",
        )
    if not details:
        details = ["No permitted knowledge evidence is available."]
    return answer.rstrip() + "\n\n---\n" + "\n\n".join(details)
