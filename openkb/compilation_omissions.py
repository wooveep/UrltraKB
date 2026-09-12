"""Publication metadata for excluded content, without exposing rejected model text."""

import json


def validate_omissions(rows):
    if not isinstance(rows, (tuple, list)):
        raise ValueError("Invalid compilation omissions")
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"stage", "reason", "items"}
            or row["stage"] not in {"facts", "planning", "generation"}
            or not isinstance(row["reason"], str)
            or not row["reason"]
            or not isinstance(row["items"], list)
            or not row["items"]
            or not all(isinstance(item, str) and item for item in row["items"])
        ):
            raise ValueError("Invalid compilation omission")
    return tuple(sorted(rows, key=lambda row: (row["stage"], row["reason"], row["items"])))


def stored_omissions(document):
    return validate_omissions(json.loads((document or {}).get("compilation_omissions", "[]")))


def omission_notice(rows):
    rows = validate_omissions(rows)
    if not rows:
        return ""
    # Identities and fixed reason codes only; rejected claims are never published.
    return (
        "\n\n## 内容遗漏\n\n"
        "> 部分内容未通过处理或核验，已排除。本次发布仅包含通过核验的内容；"
        "遗漏不代表原文没有相关信息。原件保留，可通过继续处理重新检查。\n\n"
        + "\n".join(
            "- " + json.dumps(row, ensure_ascii=False).replace("<", "&lt;").replace("]", "&#93;")
            for row in rows
        )
    )


def prune_withdrawn_links(wiki, withdrawn):
    """Remove only navigation to withdrawn pages in the private publication clone."""
    if not withdrawn:
        return
    from openkb.agent.evidence_markup import normalize_links
    from openkb.lint import _extract_wikilinks, list_existing_wiki_targets
    from openkb.locks import atomic_write_text

    existing = list_existing_wiki_targets(wiki)
    for folder in ("concepts", "entities", "summaries"):
        for path in (wiki / folder).glob("*.md"):
            content = path.read_text(encoding="utf-8")
            references = set(_extract_wikilinks(content))
            if references & withdrawn:
                cleaned = normalize_links(
                    content, existing | (references - withdrawn), {}, links_only=True
                )
                if cleaned != content:
                    atomic_write_text(path, cleaned)
