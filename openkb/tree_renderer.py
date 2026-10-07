"""Markdown renderers for PageIndex tree structures."""

from __future__ import annotations

from openkb import frontmatter


def _yaml_frontmatter(
    source_name: str, doc_id: str, description: str = "", unit_kind="page"
) -> str:
    """Return a YAML frontmatter block for a PageIndex wiki page."""
    lines = [frontmatter.kv_line("type", "Summary")]
    if description:
        lines.append(frontmatter.kv_line("description", description))
    lines.append("doc_type: pageindex")
    suffix = ".content.json" if unit_kind == "block" else ".json"
    lines.append(frontmatter.kv_line("full_text", f"sources/{source_name}{suffix}"))
    if unit_kind == "block":
        lines.append("unit_kind: block")
    return "---\n" + "\n".join(lines) + "\n---\n"


def _render_nodes_summary(nodes: list[dict], depth: int) -> str:
    """Recursively render nodes for the *summary* view (summaries only)."""
    lines: list[str] = []
    heading_prefix = "#" * min(depth, 6)
    for node in nodes:
        title = node.get("title", "")
        start = node.get("start_index", "")
        end = node.get("end_index", "")
        summary = node.get("summary", "")
        children = node.get("nodes", [])

        label = "blocks" if node.get("unit_kind") == "block" else "pages"
        lines.append(f"{heading_prefix} {title} ({label} {start}–{end})\n")
        origin = node.get("title_origin", "unknown")
        lines.append(
            f"Navigation title origin: {origin}. "
            + (
                "Generated label, not an original section heading. "
                if origin == "generated"
                else ""
            )
            + (
                "Range denotes physical pages, not section numbers.\n"
                if label == "pages"
                else "Range denotes content blocks, not section numbers.\n"
            )
        )
        if summary:
            lines.append(f"Summary: {summary}\n")
        if children:
            lines.append(_render_nodes_summary(children, depth + 1))

    return "\n".join(lines)


def render_summary_md(tree: dict, source_name: str, doc_id: str, description: str = "") -> str:
    """Render the summary Markdown page for a PageIndex tree.

    Renders each node as a heading with page range and its summary text.
    Includes a YAML frontmatter block with ``type: "Summary"`` and an
    optional ``description`` field.
    """
    frontmatter = _yaml_frontmatter(source_name, doc_id, description, tree.get("unit_kind", "page"))
    structure = tree.get("structure", [])
    body = _render_nodes_summary(structure, depth=1)
    return frontmatter + "\n" + body
