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
from openkb.state import HashRegistry


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
        for name in sorted(view.files):
            if name.startswith("sources/") and name.endswith(".json"):
                import json
                from pathlib import Path

                pages = json.loads(read_selected_file(view, name))
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


def _selected(selection: QuerySelection) -> QueryView:
    if len(selection.views) != 1:
        raise ValueError("A reader requires one bound source collection")
    return selection.views[0]


def _available(view: QueryView, path: str) -> bool:
    target = (view.scope.wiki_dir / path).resolve()
    return (
        target.is_relative_to(view.scope.wiki_dir)
        and path in view.files
        and target.is_file()
        and HashRegistry.hash_file(target) == view.files[path]
    )


def _read_error(context, error: Exception) -> str:
    from openkb.llm_execution import raise_if_terminal

    raise_if_terminal(error)
    return str(error) + " Use a valid document and range from read_file(index.md)."


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

        Read index.md first for document identifiers and their native reading locations.
        """
        if path == "index.md":
            return selection_catalog(selection)
        view = _selected(selection)
        return read_query_page(selection, path, view_id=view.view_id)

    @function_tool(failure_error_function=_read_error)
    def get_page_content(
        doc_name: str, pages: str, part: Literal["body", "notes"] | None = None
    ) -> str | list[ToolOutputText | ToolOutputImage]:
        """Read original physical pages from the knowledge base.

        For slides, part may be body or notes; null includes both without extra pages.
        """
        view = _selected(selection)
        from openkb.source_pages import read_page_selection, select_page_part

        selected = select_page_part(
            read_page_selection(
                json.loads(read_selected_file(view, f"sources/{doc_name}.json")),
                pages,
            ),
            part or None,
        )
        if any(note.startswith("No recorded content") for note in selected["diagnostics"]):
            return "Requested pages are outside the recorded range. Read index.md for valid pages."
        content = (
            view.provenance + "\nContent nature: original page extraction; OCR is transcription, "
            "and generated image interpretations are not source text.\n\n" + selected["content"]
        )
        paths = list(
            dict.fromkeys(
                image["path"] for unit in selected["units"] for image in unit.get("images", [])
            )
        )
        outputs: list[ToolOutputText | ToolOutputImage] = []
        for path in paths[:4]:
            if _available(view, path) and (view.scope.wiki_dir / path).stat().st_size <= 5_000_000:
                image = read_wiki_image(
                    path, str(view.scope.wiki_dir), expected_digest=view.files[path]
                )
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

    @function_tool(failure_error_function=_read_error)
    def get_text_content(doc_name: str, chars: str, offset: int = 0) -> str:
        """Read a frozen Unicode codepoint range START:END (0-based, end exclusive).

        Returns exact original-file locators separately from normalized text coordinates.
        Text has no physical pages. Choose doc_name from read_file(index.md); use offset=0
        initially, then the returned continuation for further reading.
        """
        import json

        from openkb.text_source import read_text_selection

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
        try:
            read_selected_file(view, path)
        except ValueError as error:
            return ToolOutputText(text=str(error))
        result = read_wiki_image(path, str(view.scope.wiki_dir), expected_digest=view.files[path])
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
        "For text sources use get_text_content/get_block_content; for worksheets "
        "use get_cell_content. These preserve original locations. "
        "Distinguish conflicting statements using the documents themselves and cite them."
        + ANSWER_SOURCE_RULES
    )
    return agent.clone(
        tools=[
            search_knowledge,
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
