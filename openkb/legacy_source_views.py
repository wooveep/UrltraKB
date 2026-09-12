"""Detached read-only views of retained local exports, without invented original coordinates."""

from urllib.parse import quote

from openkb.sources import content_id


def capture_legacy(kb_dir, entries):
    from openkb.documents import _resolve_source_file
    from openkb.legacy_pages import saved_pages_text

    views = {}
    for identity, entry in entries.items():
        if entry.get("navigation_id") or entry.get("type") not in {"short", "long_pdf"}:
            continue
        name = entry.get("doc_name") or entry.get("name", "")
        path = _resolve_source_file(kb_dir, entry, name)
        if path is None:
            continue
        text = (
            saved_pages_text(path) if path.suffix == ".json" else path.read_text(encoding="utf-8")
        )
        relative = path.relative_to(kb_dir / "wiki").as_posix()
        source_id = "legacy:" + identity
        snapshot = content_id({"source": source_id, "path": relative, "text": text})
        views[source_id] = {
            "source_id": source_id,
            "name": entry.get("name", name),
            "index": snapshot,
            "saved_snapshot": snapshot,
            "path": relative,
            "text": text,
        }
    return views


def legacy_tree(view, offset, limit):
    nodes = [
        {
            "id": "n0",
            "parent": None,
            "start": 0,
            "end": 1,
            "title": view["name"],
            "summary": "Retained local export; original coordinates are unavailable.",
        }
    ]
    return {
        "id": view["index"],
        "source_id": view["source_id"],
        "nodes": nodes[offset : offset + limit],
        "next_offset": None,
        "quality": [{"status": "saved_export", "reason": "original_coordinates_unavailable"}],
        "status": "legacy_saved",
    }


def legacy_read(view, node_id, offset, start, max_chars):
    if node_id != "n0" or offset not in (0, 1) or start > len(view["text"]):
        raise ValueError("Unknown saved source range")
    if offset == 1:
        return {"index": view["index"], "evidence": [], "next": None}
    text = view["text"][start : start + max_chars]
    end = start + len(text)
    return {
        "index": view["index"],
        "evidence": [
            {
                "reference": {
                    "source_id": view["source_id"],
                    "saved_snapshot": view["saved_snapshot"],
                    "path": view["path"],
                    "start": start,
                    "end": end,
                },
                "text": text,
                "kind": "paragraph",
                "location": {"kind": "saved_export", "path": view["path"]},
                "context": "Saved export offsets only, not original physical coordinates.",
                "assets": [],
                "citation": "[保留的原文](" + quote(view["path"], safe="/-.") + ")",
            }
        ],
        "next": {"offset": 0, "start": end} if end < len(view["text"]) else None,
    }
