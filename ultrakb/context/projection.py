"""A fixed reading selection becomes a reconstructible, namespaced navigation forest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from contextdb import ConDB, TreeDB

from ultrakb.application.query_views import QuerySelection, read_selected_file
from ultrakb.context.navigation_nodes import compact_navigation as _compact_navigation
from ultrakb.context.navigation_nodes import source_nodes
from ultrakb.lifecycle import current_generation
from ultrakb.source_navigation import published_navigation

PROJECTION_VERSION = "ultrakb-knowledge-forest-v2"


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ProjectionIdentity:
    key: str
    tree_id: str


def runtime_database(kb_dir: Path) -> Path:
    return kb_dir / ".ultrakb/context/runtime.sqlite"


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
                "retained_source_status": v.retained_source_status,
                "files": v.files,
            }
            for v in selection.views
        ],
    }


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
                **view.retained_source_status.get(path, {}),
            }
            kind = (
                "text"
                if path.endswith(".content.json")
                else ("pages" if path.endswith(".json") else "markdown")
            )
            nodes = source_nodes(data, kind, reference, published.get("structure", []))
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


def ensure_projection(selection: QuerySelection, model_identity: dict) -> ProjectionIdentity:
    if len(selection.views) != 1:
        raise ValueError("Knowledge retrieval requires one bound collection")
    if bound := selection.views[0].scope.knowledge:
        revision = bound.navigation_revision()
        return ProjectionIdentity(revision.input_fingerprint, revision.tree_id)
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
