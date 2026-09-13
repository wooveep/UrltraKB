"""PageIndex's local collection stores native indexes and their original ranges."""

import copy
import shutil
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from pageindex import IndexConfig, LocalClient
from pageindex.errors import PageIndexError
from pageindex.storage.sqlite import SQLiteStorage

from openkb.evidence import Evidence, ParseStore, complete_read_bound
from openkb.locks import atomic_write_json, kb_read_lock
from openkb.mutation import mutation_scope
from openkb.processing import processing_checkpoint
from openkb.sources import SourceStore, content_id, read_object


class PageIndexUnavailable(ValueError):
    """A database-backed published index needs repair before it can be queried."""


def _ordered(nodes):
    children = {}
    for node in nodes:
        children.setdefault(node["parent"], []).append(node)
    result = []

    def visit(parent, depth):
        for node in children.get(parent, []):
            result.append((node, depth))
            visit(node["id"], depth + 1)

    visit(None, 1)
    if len(result) != len(nodes):
        raise ValueError("PageIndex source hierarchy is incomplete")
    return result


class _NativeParser:
    """Adapt an already validated native parse to the installed parser protocol."""

    def __init__(self, record, pages, name, descriptor):
        self.record, self.pages, self.name, self.descriptor = record, pages, name, descriptor

    def supported_extensions(self):
        return [".openkb-index"]

    def parse(self, file_path, **kwargs):
        from pageindex.parser.protocol import ContentNode, ParsedDocument

        if read_object(Path(file_path)) != self.descriptor:
            raise ValueError("PageIndex input differs from the frozen source")
        ordered = _ordered(self.record["nodes"])
        inputs = []
        for i, (node, depth) in enumerate(ordered):
            processing_checkpoint()
            end = ordered[i + 1][0]["start"] if i + 1 < len(ordered) else node["end"]
            text = "\n\n".join(p["content"] for p in self.pages[node["start"] : end])
            inputs.append(
                ContentNode(text, 0, title=node["title"], index=node["start"] + 1, level=depth)
            )
        return ParsedDocument(self.name, inputs)


class _NativeStorage(SQLiteStorage):
    """Keep native coordinates and bounded summaries in the standard SDK schema."""

    def __init__(self, path, record, pages):
        self.record, self.pages = record, pages
        super().__init__(path)

    def save_document(self, collection, doc_id, doc):
        tree = copy.deepcopy(doc["structure"])
        expected = iter(_ordered(self.record["nodes"]))

        def bind(branches, parent=None):
            for branch in branches:
                node, _ = next(expected)
                if (
                    branch["title"] != node["title"]
                    or branch["line_num"] != node["start"] + 1
                    or node["parent"] != parent
                ):
                    raise ValueError("PageIndex hierarchy changed native source ranges")
                branch.update(
                    node_id=node["id"],
                    start_index=node["start"] + 1,
                    end_index=node["end"],
                    summary=node["summary"],
                    source_range={
                        key: node[key]
                        for key in ("title_origin", "summary_origin", "structure_origin")
                    },
                )
                bind(branch.get("nodes", []), node["id"])

        bind(tree)
        if next(expected, None) is not None:
            raise ValueError("PageIndex omitted native source nodes")
        super().save_document(collection, doc_id, {**doc, "structure": tree, "pages": self.pages})


def _client(kb_dir, storage):
    # All semantic work already used the bounded document execution. The SDK
    # receives explicit structure; opening/reading its local store needs no key.
    return LocalClient(
        model="openai/local-index",
        storage_path=str(kb_dir / ".openkb"),
        storage=storage,
        index_config=IndexConfig(
            if_add_node_text=False,
            if_add_node_summary=False,
            if_add_doc_description=False,
            llm_params={"api_key": "local-index-no-network"},
        ),
    )


def _database(kb_dir):
    store = SourceStore(kb_dir)
    store.owned_path(kb_dir / ".openkb/files/default")
    path = kb_dir / ".openkb/pageindex.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        store.owned_path(path.with_name(path.name + suffix))
    return path


def database_paths(kb_dir):
    database = _database(kb_dir)
    store = SourceStore(kb_dir)
    return [
        store.owned_path(database.with_name(database.name + suffix))
        for suffix in ("", "-wal", "-shm", "-journal")
    ]


@contextmanager
def _collection(kb_dir):
    path = _database(kb_dir)
    if not path.is_file():
        raise PageIndexUnavailable("PageIndex database is unavailable; rebuild its source index")
    try:
        with SQLiteStorage(str(path)) as storage:
            yield _client(kb_dir, storage).collection()
    except (
        PageIndexError,
        sqlite3.DatabaseError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
    ) as exc:
        raise PageIndexUnavailable("PageIndex database cannot read the saved source index") from exc


def save_index(kb_dir, source, parsed, record):
    """Use Collection.add and its real SQLite storage before compilation starts."""
    reader = ParseStore(kb_dir).reader(source, parsed)
    pages = []
    for block in parsed.blocks:
        processing_checkpoint()
        view = reader.read(
            Evidence(source.source_id, source.id, parsed.id, block.id),
            max_chars=complete_read_bound(block),
        )
        pages.append({"page": block.order + 1, "content": view.text})
    store = SourceStore(kb_dir)
    paths = database_paths(kb_dir)
    files = store.owned_path(kb_dir / ".openkb/files/default")
    # Existing managed files are immutable. Hardlinked backups protect the SDK
    # copy-before-parse crash window without copying every prior document.
    with mutation_scope(
        kb_dir, [*paths, files], operation="save PageIndex source index", hardlink_dirs={files}
    ) as snapshot:
        with TemporaryDirectory(prefix="pageindex-", dir=kb_dir / ".openkb") as staging:
            snapshot.track_new([Path(staging)])
            # SDK dedup is content-only. Each replacement generation must get
            # its own row; reuse is selected by the database source binding.
            descriptor = {
                "source": source.source_id,
                "version": source.id,
                "parse": parsed.id,
                "profile": record["profile"],
                "generation": uuid4().hex,
            }
            input_path = Path(staging) / "source.openkb-index"
            atomic_write_json(input_path, descriptor)
            with _NativeStorage(str(paths[0]), record, pages) as storage:
                client = _client(kb_dir, storage)
                client.register_parser(_NativeParser(record, pages, source.name, descriptor))
                documents = client.collection()
                doc_id = documents.add(str(input_path))
                tree = documents.get_document_structure(doc_id)
            saved = {
                key: value for key, value in record.items() if key not in {"nodes", "positions"}
            }
            saved.update(
                schema=1,
                pageindex={
                    "collection": "default",
                    "doc_id": doc_id,
                    "tree_digest": content_id(tree),
                },
            )
            from openkb.pageindex_bindings import save_binding

            return save_binding(paths[0], saved)


def load_nodes(kb_dir, binding):
    if (
        not isinstance(binding, dict)
        or set(binding) != {"collection", "doc_id", "tree_digest"}
        or binding["collection"] != "default"
        or not isinstance(binding["doc_id"], str)
    ):
        raise PageIndexUnavailable("Invalid PageIndex source binding")
    with kb_read_lock(kb_dir / ".openkb"), _collection(kb_dir) as documents:
        tree = documents.get_document_structure(binding["doc_id"])
    if content_id(tree) != binding["tree_digest"]:
        raise PageIndexUnavailable("PageIndex tree differs from the frozen source")
    nodes = []

    def visit(branches, parent=None):
        for node in branches:
            nodes.append(
                {
                    "id": node["node_id"],
                    "parent": parent,
                    "start": node["start_index"] - 1,
                    "end": node["end_index"],
                    "title": node["title"],
                    "summary": node["summary"],
                    **node["source_range"],
                }
            )
            visit(node.get("nodes", []), node["node_id"])

    visit(tree)
    return nodes


def indexed_reader(kb_dir, source, parsed, navigation):
    """Capture exact indexed text via Collection.get_page_content for all consumers."""
    from openkb.pageindex_evidence import IndexedEvidence

    original = ParseStore(kb_dir).reader(source, parsed)
    if not navigation or not navigation.get("pageindex"):
        raise PageIndexUnavailable("PageIndex source binding is required")
    binding = navigation["pageindex"]
    load_nodes(kb_dir, binding)
    with kb_read_lock(kb_dir / ".openkb"), _collection(kb_dir) as documents:
        # The installed SDK accepts at most 1000 ranges in one page request.
        # Native objects can far outnumber physical pages, so retain all batches.
        pages = []
        for start in range(1, len(parsed.blocks) + 1, 1000):
            processing_checkpoint()
            end = min(start + 999, len(parsed.blocks))
            pages.extend(documents.get_page_content(binding["doc_id"], f"{start}-{end}"))
    return IndexedEvidence(original, pages)


def saved_indexes(kb_dir, **filters):
    from openkb.pageindex_bindings import index_bindings

    try:
        with kb_read_lock(kb_dir / ".openkb"):
            return index_bindings(_database(kb_dir), **filters)
    except (sqlite3.DatabaseError, ValueError) as exc:
        raise PageIndexUnavailable("PageIndex source bindings are unreadable") from exc


def inventory_bindings(kb_dir):
    from openkb.pageindex_bindings import binding_inventory

    with kb_read_lock(kb_dir / ".openkb"):
        return binding_inventory(_database(kb_dir))


def index_inventory(kb_dir):
    """Semantic database state for preview-bound cleanup; legacy documents stay owned elsewhere."""
    from openkb.state import HashRegistry

    if not _database(kb_dir).exists():
        return {}
    store, result = SourceStore(kb_dir), {}
    # Cleanup fingerprints serialized SDK data. It must not interpret retired
    # tree/page payloads: a successful replacement may have left damaged history.
    with (
        kb_read_lock(kb_dir / ".openkb"),
        closing(sqlite3.connect(_database(kb_dir).as_uri() + "?mode=ro", uri=True)) as connection,
    ):
        rows = connection.execute(
            "SELECT doc_id, file_path, doc_name, doc_description, structure, pages, doc_type "
            "FROM documents WHERE collection_name='default'"
        ).fetchall()
        by_id = {row[0]: row for row in rows}
        identifiers = {row[0] for row in rows if row[-1] == "openkb-index"}
        identifiers.update(
            record["pageindex"]["doc_id"] for record in inventory_bindings(kb_dir).values()
        )
        for doc_id in sorted(identifiers):
            processing_checkpoint()
            row = by_id.get(doc_id)
            if row is not None and row[-1] != "openkb-index":
                raise ValueError("PageIndex source binding refers to another document type")
            directory = managed_index_directories(kb_dir, [doc_id])[0]
            expected = directory.with_suffix(".openkb-index")
            path = store.owned_path(Path(row[1]) if row else expected)
            if path != expected:
                raise ValueError("Invalid PageIndex managed input path")
            contents = {}
            directories = [directory.relative_to(kb_dir).as_posix()] if directory.is_dir() else []
            for artifact in [path, *sorted(directory.rglob("*"))]:
                processing_checkpoint()
                artifact = store.owned_path(artifact)
                relative = artifact.relative_to(kb_dir).as_posix()
                if artifact.is_dir():
                    directories.append(relative)
                elif artifact.is_file():
                    contents[relative] = HashRegistry.hash_file(artifact)
            result[doc_id] = {
                "files": contents,
                "directories": directories,
                "document_digest": content_id(row),
            }
    return result


def delete_indexes(kb_dir, doc_ids):
    """Caller snapshots database and listed managed files inside the KB mutation."""
    if doc_ids:
        with _collection(kb_dir) as documents:
            existing = {row["doc_id"] for row in documents.list_documents()}
            for doc_id in doc_ids:
                processing_checkpoint()
                if doc_id in existing:
                    documents.delete_document(doc_id)
                else:
                    # A lost SDK row can leave its declared binding and managed
                    # files behind. Preview and mutation cover these same paths.
                    directory = managed_index_directories(kb_dir, [doc_id])[0]
                    directory.with_suffix(".openkb-index").unlink(missing_ok=True)
                    if directory.exists():
                        shutil.rmtree(directory)
        with closing(sqlite3.connect(_database(kb_dir))) as connection, connection:
            connection.executemany(
                "DELETE FROM openkb_source_indexes WHERE doc_id = ?",
                [(doc_id,) for doc_id in doc_ids],
            )


def managed_index_directories(kb_dir, doc_ids):
    store = SourceStore(kb_dir)
    result = []
    for doc_id in doc_ids:
        if not isinstance(doc_id, str) or str(UUID(doc_id)) != doc_id:
            raise ValueError("Invalid PageIndex document identity")
        result.append(store.owned_path(kb_dir / ".openkb/files/default" / doc_id))
    return result
