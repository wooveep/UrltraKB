"""A fixed reading selection becomes a reconstructible, namespaced navigation forest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from contextdb import ConDB, TreeDB

from openkb.application.query_views import QuerySelection, read_selected_file
from openkb.lifecycle import current_generation
from openkb.source_navigation import published_navigation

PROJECTION_VERSION = "openkb-knowledge-forest-v2"


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ProjectionIdentity:
    key: str
    tree_id: str


def runtime_database(kb_dir: Path) -> Path:
    return kb_dir / ".openkb/context/runtime.sqlite"


def open_runtime(kb_dir: Path) -> TreeDB:
    path = runtime_database(kb_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = TreeDB(str(path))
    with db.transaction() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS okb_projections (
            projection_key TEXT PRIMARY KEY, tree_id TEXT NOT NULL REFERENCES trees(tree_id),
            policy TEXT NOT NULL)""")
    return db


def selection_identity(selection: QuerySelection) -> dict:
    return {
        "policy": PROJECTION_VERSION,
        "views": [
            {
                "kb": str(v.scope.kb_dir),
                "kb_generation": current_generation(v.scope.kb_dir),
                "generation": v.head_generation,
                "knowledge_revision_id": v.knowledge_revision_id,
                "source_revisions": v.source_revisions_by_path,
                "files": v.files,
            }
            for v in selection.views
        ],
    }


def _leaf(title: str, reference: dict, native: dict) -> dict:
    reference = {**reference, "native_location": native}
    return {
        "title": title,
        "rel_path": title,
        "is_dir": False,
        "source_ref": reference,
        "content": "Original source locator; reread before use.",
    }


def _indexed_nodes(structure: list[dict], reference: dict, kind: str) -> dict:
    nodes = {}
    for ordinal, node in enumerate(structure):
        start, end = node.get("start_index"), node.get("end_index")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end:
            raise ValueError("Published navigation has invalid native positions")
        title = (
            reference["doc_name"]
            + " / "
            + str(node.get("title", ""))
            + " — navigation: "
            + str(node.get("summary", ""))[:800]
        )
        children = _indexed_nodes(node.get("nodes", []), reference, kind)
        key = str(node.get("node_id", ordinal))
        nodes[key] = (
            {"title": title, "rel_path": title, "is_dir": True, "children": children}
            if children
            else _leaf(title, reference, {kind: f"{start}-{end}"})
        )
    return nodes


def knowledge_forest(selection: QuerySelection) -> dict:
    documents = {}
    for view in selection.views:
        names = sorted(
            n
            for n in view.files
            if n.startswith("sources/")
            and n.endswith((".json", ".md"))
            and not n.startswith("sources/images/")
        )
        richer = {
            Path(n).name.removesuffix(".content.json").removesuffix(".json")
            for n in names
            if n.endswith(".json")
        }
        for path in names:
            doc_name = (
                Path(path)
                .name.removesuffix(".content.json")
                .removesuffix(".json")
                .removesuffix(".md")
            )
            if path.endswith(".md") and doc_name in richer:
                continue
            data = read_selected_file(view, path)
            published = published_navigation(
                view.scope.kb_dir,
                view.view_id,
                doc_name,
                knowledge_revision_id=view.knowledge_revision_id,
                source_revision_id=view.source_revisions_by_path.get(path),
            )
            if published and published["source_revision_id"] != view.source_revisions_by_path.get(
                path
            ):
                published = {}  # A projection never borrows navigation from a different revision.
            reference = {
                "doc_name": doc_name,
                "path": path,
                "content_digest": view.files[path],
                "source_revision_id": view.source_revisions_by_path.get(path),
                "unit_id": published.get("unit_id"),
                "unit_revision_id": published.get("unit_revision_id"),
                "knowledge_revision_id": published.get("knowledge_revision_id")
                or view.knowledge_revision_id,
            }
            if path.endswith(".content.json"):
                from openkb.text_source import read_text_selection

                raw = json.loads(data)
                source = read_text_selection(raw)
                if source.get("block_count"):
                    from openkb.block_package import read_block_selection

                    nodes = _indexed_nodes(published.get("structure", []), reference, "blocks")
                    if not nodes:
                        nodes = {
                            str(b["ordinal"]): _leaf(
                                doc_name + " / " + b["content"][:400],
                                reference,
                                {"blocks": str(b["ordinal"])},
                            )
                            for b in read_block_selection(raw)["units"]
                        }
                else:
                    nodes = _text_nodes(raw, reference)
            elif path.endswith(".json"):
                from openkb.source_pages import read_page_selection

                raw = read_page_selection(json.loads(data))["units"]
                nodes = _indexed_nodes(published.get("structure", []), reference, "pages")
                if not nodes:
                    nodes = {
                        str(p["page"]): _leaf(
                            doc_name
                            + " / physical page "
                            + str(p["page"])
                            + " / "
                            + p["content"][:500],
                            reference,
                            {"pages": str(p["page"])},
                        )
                        for p in raw
                    }
            else:
                text = data.decode("utf-8")
                nodes = {
                    "source": _leaf(
                        doc_name + " / " + text[:500], reference, {"chars": f"0:{len(text)}"}
                    )
                }
            if nodes:
                documents[fingerprint(reference)] = {
                    "title": doc_name,
                    "rel_path": doc_name,
                    "doc_name": doc_name,
                    "is_dir": True,
                    "children": nodes,
                }
    return {
        "title": "Knowledge navigation",
        "is_dir": True,
        "children": {key: _compact_navigation(doc) for key, doc in documents.items()},
    }


def _compact_navigation(node: dict) -> dict:
    """Keep every result locator and ancestor title, dropping unary structure only."""
    node = dict(node)
    if "children" in node:
        node["children"] = {
            key: _compact_navigation(child) for key, child in node["children"].items()
        }
    while node.get("is_dir") and not node.get("source_ref") and len(node.get("children", {})) == 1:
        child = next(iter(node["children"].values()))
        title = node["title"] + " / " + child["title"]
        node = {**node, **child, "title": title, "rel_path": title}
        if not child.get("children"):
            node.pop("children", None)
    return node


def _text_nodes(raw: dict, reference: dict) -> dict:
    text, nodes = raw["text"], {}
    origins = raw.get("origins", [])
    cells = [o for o in origins if o.get("sheet_cell")]
    if cells:
        for start in range(0, len(cells), 12):
            chunk = cells[start : start + 12]
            coordinates = [o["sheet_cell"]["cell"]["coordinate"] for o in chunk]
            a, b = chunk[0]["normalized_span"][0], chunk[-1]["normalized_span"][1]
            nodes[str(start)] = _leaf(
                reference["doc_name"] + " / " + ",".join(coordinates) + " / " + text[a:b][:500],
                reference,
                {"cells": ",".join(coordinates)},
            )
    else:
        for start in range(0, len(text), 4000):
            end = min(len(text), start + 4000)
            nodes[str(start)] = _leaf(
                reference["doc_name"] + f" / chars {start}:{end} / " + text[start : start + 500],
                reference,
                {"chars": f"{start}:{end}"},
            )
    return nodes


def ensure_projection(selection: QuerySelection, model_identity: dict) -> ProjectionIdentity:
    if len(selection.views) != 1:
        raise ValueError("Knowledge retrieval requires one bound collection")
    key = fingerprint(
        {**selection_identity(selection), "retrieval": "beam-v1", "models": model_identity}
    )
    kb_dir = selection.views[0].scope.kb_dir
    with open_runtime(kb_dir) as db:
        row = db.conn.execute(
            "SELECT tree_id, policy FROM okb_projections WHERE projection_key=?", (key,)
        ).fetchone()
        if (
            row
            and row[1] == PROJECTION_VERSION
            and db.conn.execute("SELECT 1 FROM trees WHERE tree_id=?", (row[0],)).fetchone()
        ):
            return ProjectionIdentity(key, row[0])
        forest = knowledge_forest(selection)
        if not forest["children"]:
            raise ValueError("No readable originals in the selected collection")
        with db.transaction() as connection:
            tree_id = ConDB(storage=db).store(
                forest,
                format="generic",
                meta={"source": "filesystem", "policy": PROJECTION_VERSION},
            )
            connection.execute(
                "INSERT OR REPLACE INTO okb_projections VALUES (?, ?, ?)",
                (key, tree_id, PROJECTION_VERSION),
            )
        return ProjectionIdentity(key, tree_id)
