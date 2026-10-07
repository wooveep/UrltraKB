"""Attach independent, pinned read tools to query, chat and skill agents."""

import json
from typing import Literal

from agents import ToolOutputImage, ToolOutputText, function_tool

from openkb.agent.answer_evidence import ANSWER_EVIDENCE_RULES
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


def restrict_query_agent(agent, selection: QuerySelection):
    """Replace every wiki reader; preserving artifact writers grants no extra knowledge reads."""
    originals: dict[tuple[str, str], str] = {}

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
        content = read_query_page(selection, path, view_id=view.view_id)
        if path.startswith("sources/") and _available(view, path):
            originals[view.view_id, path] = content
        return content

    @function_tool
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
        locator = f"sources/{doc_name}.json pages={pages} part={part}"
        from openkb.source_pages import read_page_selection, select_page_part

        selected = select_page_part(
            read_page_selection(
                json.loads((view.scope.wiki_dir / f"sources/{doc_name}.json").read_text("utf-8")),
                pages,
            ),
            part,
        )
        originals[view.view_id, locator] = selected["content"]
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
                    outputs.extend(
                        [
                            ToolOutputText(text=f"Original page image: {path}"),
                            ToolOutputImage(image_url=result["image_url"]),
                        ]
                    )
        content += f"\nEvidence locator: {locator}"
        if paths:
            content += "\nInspect attached images before concluding information is missing. "
            content += "Use get_image for relevant images not attached: " + ", ".join(paths)
        return [ToolOutputText(text=content), *outputs] if outputs else content

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
        locator = f"{path} chars={chars}"
        originals[view.view_id, locator] = selected["content"]
        return (
            view.provenance
            + "\n\n"
            + json.dumps(selected, ensure_ascii=False)
            + f"\nEvidence locator: {locator}"
        )

    @function_tool
    def get_block_content(doc_name: str, blocks: str, view_id: str = "") -> str:
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
        originals[view.view_id, locator] = selected["content"]
        return (
            view.provenance
            + "\n\n"
            + json.dumps(selected, ensure_ascii=False)
            + f"\nEvidence locator: {locator}"
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

    @function_tool
    def record_source_fact(
        subject: str, setting: str, condition: str, quote: str, locator: str, view_id: str = ""
    ) -> str:
        """Record one rule before combining sources. Keep subject/setting/condition separate.

        quote must be verbatim from an original read, not a summary. locator must
        match that read's evidence locator. Use an empty condition if none is stated.
        Copy subject, setting and condition verbatim from the SAME quote, including
        enough context to identify the subject. Do not borrow another rule's condition.
        Recording a quote verifies its origin, not the interpretation of the rule.
        """
        view = _selected(selection, view_id)
        from openkb.agent.answer_evidence import original_quote

        locator = locator.removeprefix("Evidence locator:").strip()
        original = originals.get((view.view_id, locator), "")
        matched_quote = original_quote(quote, original)
        if matched_quote is None:
            return (
                "Unverified source fact: quote/locator does not match an original read. "
                "Read the source again."
            )
        quote = matched_quote
        if (
            not subject.strip()
            or not setting.strip()
            or any(value not in quote for value in (subject, setting, condition))
        ):
            return (
                "Unverified source fact: subject, setting and condition must come from "
                "the same exact quote. Do not transfer conditions between rules."
            )
        return json.dumps(
            {
                "subject": subject,
                "setting": setting,
                "condition": condition,
                "quote": quote,
                "locator": locator,
                "view_id": view.view_id,
            },
            ensure_ascii=False,
        )

    readers = {
        "record_source_fact",
        "read_file",
        "get_page_content",
        "get_block_content",
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
        + ANSWER_EVIDENCE_RULES
        + selection_catalog(selection)
    )
    return agent.clone(
        tools=[
            read_file,
            get_page_content,
            get_text_content,
            get_block_content,
            get_image,
            record_source_fact,
            *extra_tools,
        ],
        instructions=(agent.instructions or "") + instructions,
    )


def evidence_answer(answer: str, selection: QuerySelection) -> str:
    """Label permitted immutable revisions; this list does not track actual citations."""
    from openkb.agent.answer_evidence import unsupported_version_claims, version_rejection

    violations = unsupported_version_claims(answer, selection)
    if violations:
        answer = version_rejection(violations)
    if not selection.views and not answer.strip():
        answer = (
            "尚未确定可用的产品或版本证据范围，本次未生成知识答案。"
            "请使用资料中的产品名称、选择知识视图，或确认产品别名。"
        )
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
    return answer.rstrip() + "\n\n---\n本次允许的证据范围\n\n" + "\n\n".join(details)
