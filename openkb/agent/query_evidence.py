"""Attach independent, pinned read tools to query, chat and skill agents."""

import json
from typing import Literal

from agents import ToolOutputImage, ToolOutputText, function_tool

from openkb.agent.tools import read_wiki_image
from openkb.application.query_views import (
    QuerySelection,
    QueryView,
    read_query_page,
    read_selected_file,
    selection_current,
)
from openkb.source_context import ANSWER_SOURCE_RULES


def evidence_input(input_data):
    """Preserve conversation context; source readers remain pinned per request."""
    return input_data


def selection_catalog(selection: QuerySelection) -> str:
    lines = [*selection.missing]
    for view in selection.views:
        lines.append("Knowledge base document catalog")
        if "index.md" in view.files:
            lines.append(read_query_page(selection, "index.md", view_id=view.view_id))
        else:
            lines.extend(f"  {name}" for name in sorted(view.files) if name.endswith(".md"))
        from openkb.source_reading import source_capabilities

        for source in source_capabilities(view).values():
            lines.append(json.dumps(source, ensure_ascii=False))
    return "\n".join(lines) or "No permitted knowledge evidence is available."


def _selected(selection: QuerySelection) -> QueryView:
    if len(selection.views) != 1:
        raise ValueError("A reader requires one bound source collection")
    return selection.views[0]


def _read_error(context, error: Exception) -> str:
    from openkb.llm_execution import raise_if_terminal

    raise_if_terminal(error)
    from agents.exceptions import ModelBehaviorError

    from openkb.source_pages import PageRangeError
    from openkb.source_reading import CATALOG_ACTION, ReaderFailure

    code, message, action = (
        "invalid_arguments",
        "Use the tool's declared argument types.",
        CATALOG_ACTION,
    )
    if isinstance(error, ReaderFailure):
        code, message, action = error.code, error.message, error.action
    elif "changed after selection" in str(error):
        code, message = "stale_snapshot", "Evidence changed after selection; start a new question."
    elif "missing" in str(error).lower() or "not found" in str(error).lower():
        code, message = "content_not_found", "Document not found or image missing in this snapshot."
    elif isinstance(error, PageRangeError):
        code, message = "range_out_of_bounds", "Choose a recorded native range from index.md."
    elif not isinstance(error, (ModelBehaviorError, ValueError, TypeError)):
        code, message = "read_failed", "The source could not be read; use the catalog to recover."
    return json.dumps(
        {"status": "error", "reason_code": code, "message": message, "reading_action": action},
        ensure_ascii=False,
    )


def _original_payload(selected: dict, offset: int) -> dict:
    from openkb.agent.evidence_payload import project_evidence

    return {
        **project_evidence(selected, offset=offset),
        "content_kind": "original_source",
        "interpretation": "Original extraction and recorded native locators; generated "
        "image interpretations are not original text. Partial reading does not establish absence.",
    }


def restrict_query_agent(agent, selection: QuerySelection):
    """Replace every wiki reader; preserving artifact writers grants no extra knowledge reads."""
    if len(selection.views) > 1:
        raise ValueError("Multiple source collections are unsupported; bind one at the application")

    from openkb.context.knowledge import KnowledgeRetrieval
    from openkb.llm_execution import active_executor
    from openkb.source_reading import require_reader, source_capabilities

    sources = source_capabilities(_selected(selection)) if selection.views else {}
    available = {tool for source in sources.values() for tool in source["readers"]}
    executor = active_executor()
    retrieval = KnowledgeRetrieval(selection, executor) if executor else None

    @function_tool(failure_error_function=_read_error)
    async def search_knowledge(query: str) -> str:
        """Find relevant originals in this knowledge base and read bounded native locations.

        Use a complete question. Returned coverage is partial; follow original reading actions
        for unread material. Missing candidates do not establish that a fact does not exist.
        """
        from openkb.context.workers import run_blocking

        result = (
            await run_blocking(lambda: retrieval.search(query))
            if retrieval
            else {
                "status": "unavailable",
                "hits": [],
                "diagnostics": ["Use read_file(index.md) and the original readers"],
            }
        )
        return json.dumps(result, ensure_ascii=False)

    @function_tool(failure_error_function=_read_error)
    def read_file(path: str) -> str:
        """Read a permitted Markdown page in the current knowledge base.

        Use index.md for document identifiers and executable native reading actions.
        """
        if path == "index.md":
            return selection_catalog(selection)
        view = _selected(selection)
        read_selected_file(view, path)
        return read_query_page(selection, path, view_id=view.view_id)

    def read_pages(doc_name, pages, tool, part=None):
        source = require_reader(sources, doc_name, tool)
        view = _selected(selection)
        from openkb.source_pages import read_page_selection, select_page_part
        from openkb.source_reading import ReaderFailure

        selected = select_page_part(
            read_page_selection(json.loads(read_selected_file(view, source["path"])), pages), part
        )
        if any(note.startswith("No recorded content") for note in selected["diagnostics"]):
            raise ReaderFailure(
                "range_out_of_bounds",
                "Requested pages are outside the recorded range.",
                source["reading_action"],
            )
        content = (
            view.provenance + "\nContent nature: original page extraction; OCR is transcription, "
            "and generated image interpretations are not source text.\n\n" + selected["content"]
        )
        paths = list(
            dict.fromkeys(
                image["path"] for unit in selected["units"] for image in unit.get("images", [])
            )
        )
        if paths:
            content += "\nImage pixels not read. Use get_image for relevant images: " + ", ".join(
                paths
            )
        return content

    @function_tool(failure_error_function=_read_error)
    def get_page_content(doc_name: str, pages: str) -> str:
        """Read recorded physical pages, using a page range such as 1-3.

        Image references are not read pixels. Use the returned image actions when relevant.
        """
        return read_pages(doc_name, pages, "get_page_content")

    @function_tool(failure_error_function=_read_error)
    def get_slide_content(doc_name: str, slides: str, part: Literal["all", "body", "notes"]) -> str:
        """Read recorded slides with their complete content, visible body, or speaker notes."""
        return read_pages(doc_name, slides, "get_slide_content", None if part == "all" else part)

    @function_tool(failure_error_function=_read_error)
    def get_text_content(doc_name: str, chars: str, offset: int = 0) -> str:
        """Read a frozen Unicode codepoint range START:END (0-based, end exclusive).

        Returns exact original-file locators separately from normalized text coordinates.
        Text has no physical pages. Choose doc_name from read_file(index.md); use offset=0
        initially, then the returned continuation for further reading.
        """
        import json

        from openkb.text_source import read_text_selection

        require_reader(sources, doc_name, "get_text_content")
        view = _selected(selection)
        path = f"sources/{doc_name}.content.json"
        selected = read_text_selection(json.loads(read_selected_file(view, path)), chars)
        return (
            view.provenance
            + "\n\n"
            + json.dumps(_original_payload(selected, offset), ensure_ascii=False)
        )

    @function_tool(failure_error_function=_read_error)
    def get_block_content(doc_name: str, blocks: str, offset: int = 0) -> str:
        """Read frozen blocks, original ranges and display context in one allowed view.

        blocks uses one-based ordinals (e.g. 1,3-5). These are not physical pages.
        Use offset=0 initially, then the returned continuation.
        """
        import json

        from openkb.block_package import read_block_selection

        require_reader(sources, doc_name, "get_block_content")
        view = _selected(selection)
        path = f"sources/{doc_name}.content.json"
        selected = read_block_selection(json.loads(read_selected_file(view, path)), blocks)
        return (
            view.provenance
            + "\n\n"
            + json.dumps(_original_payload(selected, offset), ensure_ascii=False)
        )

    @function_tool(failure_error_function=_read_error)
    def get_cell_content(doc_name: str, cells: str, offset: int = 0) -> str:
        """Read finite worksheet ranges in A1 notation, retaining cell locations.

        Use offset=0 initially; repeat the same range with offset=continuation.
        """
        from openkb.text_source import read_text_selection
        from openkb.workbooks.selection import select_cells

        require_reader(sources, doc_name, "get_cell_content")
        view = _selected(selection)
        path = f"sources/{doc_name}.content.json"
        full = read_text_selection(json.loads(read_selected_file(view, path)))
        selected = select_cells(full, cells)
        return (
            view.provenance
            + "\n\n"
            + json.dumps(_original_payload(selected, offset), ensure_ascii=False)
        )

    @function_tool(failure_error_function=_read_error)
    def search_originals(terms: list[str], doc_name: str = "") -> str:
        """Locate phrases in the current sources; read the matching original next.

        Use doc_name="" to search all permitted documents, or an identifier from index.md.
        Empty results only mean this literal search found no matches.
        """
        from openkb.agent.evidence_search import search_originals as search

        return json.dumps(search(selection, terms, doc_name=doc_name), ensure_ascii=False)

    @function_tool(failure_error_function=_read_error)
    def get_image(image_path: str) -> ToolOutputImage | ToolOutputText:
        """View a retained image at a location returned by an original source reader."""
        view = _selected(selection)
        path = image_path if image_path in view.files else f"sources/{image_path}"
        read_selected_file(view, path)
        result = read_wiki_image(path, str(view.scope.wiki_dir), expected_digest=view.files[path])
        return (
            ToolOutputImage(image_url=result["image_url"])
            if result["type"] == "image"
            else ToolOutputText(text=result["text"])
        )

    readers = {
        "read_file",
        "get_page_content",
        "get_slide_content",
        "get_block_content",
        "get_text_content",
        "get_image",
        "get_cell_content",
        "search_originals",
        "search_knowledge",
        "record_source_fact",
        "read_wiki_file",
        "list_wiki_dir",
        "query_wiki",
    }
    extra_tools = [tool for tool in agent.tools if getattr(tool, "name", None) not in readers]
    instructions = (
        "\n\n# Reading this knowledge base\n"
        "Begin ordinary knowledge questions with search_knowledge(query). It uses the "
        "published navigation to select multiple candidates and rereads their originals. "
        "If you already have a valid exact source location, you may read it directly. "
        "Generated navigation only locates evidence; it does not certify its statements. "
        "Use read_file(index.md) for the document and concept index. "
        "Execute the reading_action returned by the catalog or retrieval using its exact "
        "tool and arguments. Only registered tools apply to this source collection. "
        "Distinguish conflicting statements using the documents themselves and cite them."
        + ANSWER_SOURCE_RULES
        + "\n# End knowledge-base reading\n"
    )
    native = [
        get_page_content,
        get_slide_content,
        get_text_content,
        get_block_content,
        get_cell_content,
    ]
    selected_tools = [
        search_knowledge,
        read_file,
        *[tool for tool in native if tool.name in available],
    ]
    if sources:
        selected_tools.append(search_originals)
    if selection.views and any(
        name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
        for name in selection.views[0].files
    ):
        selected_tools.append(get_image)
    base_instructions, marker, previous = (agent.instructions or "").partition(
        "\n\n# Reading this knowledge base"
    )
    if marker:
        base_instructions += previous.partition("\n# End knowledge-base reading\n")[2]
    return agent.clone(
        tools=[*selected_tools, *extra_tools], instructions=base_instructions + instructions
    )


def evidence_answer(answer: str, selection: QuerySelection) -> str:
    """Keep the generated answer; source metadata does not rewrite its claims."""
    if not selection.has_evidence:
        return "知识库中没有可读取的原文，请先导入资料。"
    if not selection_current(selection):
        return answer.rstrip() + "\n\n回答期间资料发生变更，请重新提问以读取当前内容。"
    return answer
