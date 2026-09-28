"""Legacy PDF indexing adapted to the current immutable source/navigation handoff."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from openkb.navigation_evidence import verified_reader
from openkb.pdf_navigation_pages import PDFPages
from openkb.pdf_navigation_requests import PDFContentError, PDFRequests
from openkb.pdf_navigation_runtime import LegacyPDF
from openkb.sources import content_id


def _run(coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    context = copy_context()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(context.run, asyncio.run, coroutine).result()


def _nodes(tree, pages, summaries, description="", *, issues):
    nodes = [
        {
            "id": "n0",
            "parent": None,
            "start": 0,
            "end": len(pages.parsed.blocks),
            "title": pages.source.name,
            "title_origin": "source",
            "summary": description,
            "summary_origin": "model" if description else "unavailable",
            "structure_origin": "basic",
        }
    ]

    def visit(branches, parent):
        for branch in branches:
            first, last = branch.get("start_index"), branch.get("end_index")
            if not (
                type(first) is int and type(last) is int and 1 <= first <= last <= len(pages.values)
            ):
                issues.append({"task": "page_boundary", "reason": "invalid_legacy_pdf_page_range"})
                visit(branch.get("nodes", []), parent)
                continue
            bounds = pages.bounds(first, last)
            if bounds is None:
                visit(branch.get("nodes", []), parent)
                continue  # Empty physical pages have no saved body to select.
            title = branch.get("title")
            if not isinstance(title, str) or not title.strip():
                visit(branch.get("nodes", []), parent)
                continue
            summary = branch.get("summary") or ""
            identity = (
                "n"
                + content_id(
                    {
                        "parent": parent,
                        "ordinal": len(nodes),
                        "title": title,
                        "pages": [first, last],
                    }
                )[:24]
            )
            node = {
                "id": identity,
                "parent": parent,
                "start": bounds[0],
                "end": bounds[1],
                "title": title,
                "title_origin": "inferred" if title == "Preface" else "source",
                "summary": summary,
                "summary_origin": "model" if summary else "unavailable",
                "structure_origin": "inferred",
                "pdf_page_range": {"start": first, "end": last},
                "summary_details": {
                    "status": "complete"
                    if summary
                    else "insufficient_evidence"
                    if summaries
                    else "not_requested",
                    "reason": None if summary or not summaries else "pdf_summary_unavailable",
                    "covered_ranges": [list(bounds)] if summary else [],
                    "basis": "original",
                },
            }
            nodes.append(node)
            visit(branch.get("nodes", []), identity)

    visit(tree, "n0")
    return nodes


def infer_pdf(kb, source, parsed, record, settings, bundle, allowance, checkpoints, *, reader=None):
    reader = verified_reader(kb, source, parsed, reader)
    pages = PDFPages(kb, source, parsed, settings["model"], reader)
    transport = PDFRequests(pages, settings, bundle, allowance, checkpoints, record["profile"])
    runtime = LegacyPDF(pages, transport)
    summaries = (settings.get("navigation") or {}).get("summaries", True)
    try:
        tree, description = _run(runtime.run(settings["model"], summaries))
        nodes = _nodes(tree, pages, summaries, description, issues=runtime.issues)
        from openkb.navigation_tree import validate_nodes

        try:
            validate_nodes(nodes, len(parsed.blocks))
        except ValueError as exc:
            raise PDFContentError("invalid_legacy_pdf_tree") from exc
        record["nodes"] = nodes
    except PDFContentError as exc:
        runtime.issues.append({"task": "pdf_navigation", "reason": str(exc)})
    if runtime.issues:
        record.update(status="degraded", reason="pdf_navigation_partial")
    record["windows"] = pages.manifest(runtime.reading_groups(), record.get("reason"))
    if runtime.issues and record["windows"]:
        record["windows"][0]["structure_issues"] = {
            "reason": "pdf_navigation_partial",
            "count": len(runtime.issues),
            "candidates": runtime.issues,
        }
