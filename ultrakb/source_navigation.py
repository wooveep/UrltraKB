"""Read published PageIndex navigation and identity without indexing again."""


def planning_navigation(structure: list[dict]) -> list[dict]:
    """Expose retained topic locators and hierarchy without repeating subtrees."""
    result: list[dict] = []

    def visit(nodes: list[dict], parent_id: str | None = None) -> None:
        for node in nodes:
            entry = {
                key: node[key]
                for key in ("node_id", "title", "start_index", "end_index")
                if key in node
            }
            summary = node.get("summary")
            if isinstance(summary, str) and summary.strip():
                entry["navigation_summary"] = summary
            if parent_id is not None:
                entry["parent_node_id"] = parent_id
            result.append(entry)
            visit(node.get("nodes", []), node.get("node_id", parent_id))

    visit(structure)
    return result
