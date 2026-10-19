"""Real process exits at native CAS, publication and working-copy boundaries."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import pytest
from figure_fixtures import png_bytes

from ultrakb.application.knowledge_bases import initialize_kb
from ultrakb.application.views import published_scope
from ultrakb.compile_checkpoints import digest
from ultrakb.processing_policy import classify_markdown_tokens
from ultrakb.storage.catalog_store import CatalogStore
from ultrakb.storage.checkpoint_store import CompileCheckpointStore, CompileWork
from ultrakb.storage.database import Database, DatabaseIdentity
from ultrakb.storage.execution_store import ExecutionStore
from ultrakb.storage.index_store import IndexStore
from ultrakb.storage.knowledge_store import KnowledgeStore
from ultrakb.storage.pending_store import PendingStore
from ultrakb.storage.wiki_projection import ProjectionStore

EXIT_CODE = 42


def _retain(root, label):
    identity = published_scope(root).knowledge.identity
    source = root.parent / "manual.md"
    image = root.parent / "figure.png"
    source.write_text(f"{label} original\n![figure](images/figure.png)")
    image.write_bytes(png_bytes("red" if label == "OLD" else "blue"))
    catalog = CatalogStore(root, identity)
    admitted = catalog.admit(source, source_key="one-source", sidecars={"figure.png": image})
    processing = catalog.retain_processing(
        admitted.revision_id,
        {"body.md": source, "images/figure.png": image},
        body_path="body.md",
        fingerprint="native-crash-fixture",
        operation_id=label,
        decision=classify_markdown_tokens(10),
    )
    return catalog.plan_unit(processing.processing_id, key="body", doc_name="manual")


def _files(root, target, label):
    identity = published_scope(root).knowledge.identity
    catalog = CatalogStore(root, identity)
    processing = catalog.processing(target.processing_id)
    return {
        "index.md": f"{label} index".encode(),
        "concepts/topic.md": (
            f"---\nsources: [sources/manual.md]\n---\n{label} committed claim"
        ).encode(),
        "sources/manual.md": catalog.read_processed(processing.processing_id, "body.md"),
        "sources/images/figure.png": catalog.read_processed(
            processing.processing_id, "images/figure.png"
        ),
    }


def _crash_publication(root, spec_path, evidence_path, point, timing):
    def forbid_network(event, args):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise AssertionError("The crash protocol uses no network or model service")

    sys.addaudithook(forbid_network)
    spec = json.loads(spec_path.read_text())
    identity = DatabaseIdentity(**spec["identity"])
    catalog = CatalogStore(root, identity)
    target = catalog.unit_revision(spec["target"])
    store, pending = KnowledgeStore(root, identity), PendingStore(root, identity)
    index = IndexStore(root, identity).accept(
        target.unit_revision_id,
        {
            "doc_name": "manual",
            "pages": [{"page": 1, "content": "NEW index content"}],
            "structure": [
                {
                    "node_id": "root",
                    "title": "Manual",
                    "text": "NEW index content",
                    "start_index": 1,
                    "end_index": 1,
                }
            ],
        },
        content_fingerprint=digest("new-index"),
    )
    execution = ExecutionStore(root, identity).begin(
        "crash-fixture", fingerprint="f" * 64, max_calls=0
    )
    work = CompileCheckpointStore(root, identity).for_work(
        execution.model_budget_id,
        object_id={"unit_revision_id": target.unit_revision_id},
        conditions={"compiler": 3},
        execution_id=execution.execution_id,
    )
    work.save("summary:1", digest("accepted-input"), {"content": "Accepted before publication"})
    job = pending.enqueue(target.source_revision_id, unit_revision_id=target.unit_revision_id)
    job = pending.start(pending.claim(intent_id=job.intent_id))
    pending.bind_execution(job, execution)
    evidence_path.write_text(
        json.dumps(
            {
                "work_id": work.work_id,
                "budget_id": work.model_budget_id,
                "object_digest": work.object_digest,
                "execution_id": work.execution_id,
                "index_revision_id": index.index_revision_id,
            }
        )
    )
    files = _files(root, target, "NEW")
    import ultrakb.storage.wiki_projection as projection
    from ultrakb.storage import blobs

    write_blob, replace, unlink = blobs.atomic_write_bytes, os.replace, projection.durable_unlink
    transaction = Database.transaction
    prepared = None

    def exit_now():
        os._exit(EXIT_CODE)

    def blob_write(path, payload):
        selected = point == "blob" and payload == files["concepts/topic.md"]
        if selected and timing == "before":
            exit_now()
        result = write_blob(path, payload)
        if selected and timing == "after":
            exit_now()
        return result

    @contextmanager
    def commit(db):
        with transaction(db) as sql:
            yield sql
            selected = (
                prepared is not None
                and point == "publication"
                and sql.execute(
                    "SELECT publication_id FROM ukb_heads WHERE view_id = 'legacy'"
                ).fetchone()
                == (prepared.publication_id,)
            )
            if selected and timing == "before":
                exit_now()
        if selected and timing == "after":
            exit_now()

    def replace_file(source, destination, *args, **options):
        selected = (
            point == "projection-replace" and Path(destination) == root / "wiki/concepts/topic.md"
        )
        if selected and timing == "before":
            exit_now()
        result = replace(source, destination, *args, **options)
        if selected and timing == "after":
            exit_now()
        return result

    def unlink_file(path, **options):
        selected = point == "projection-delete" and path == root / "wiki/concepts/obsolete.md"
        if selected and timing == "before":
            exit_now()
        result = unlink(path, **options)
        if selected and timing == "after":
            exit_now()
        return result

    blobs.atomic_write_bytes = blob_write
    Database.transaction = commit
    os.replace = replace_file
    projection.durable_unlink = unlink_file
    prepared = store.prepare_revision(
        files,
        expected=spec["old_publication"],
        unit_revisions=(target.unit_revision_id,),
        index_revisions=(index.index_revision_id,),
        import_work=job,
    )
    store.publish(prepared, execution_id=execution.execution_id)
    ProjectionStore(root, identity).sync()
    raise AssertionError("The selected crash boundary was never reached")


@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize(
    "point", ["blob", "publication", "projection-replace", "projection-delete"]
)
def test_process_death_exposes_one_complete_publication(tmp_path, point, timing):
    root = tmp_path / "kb"
    initialize_kb(root, seed_environment=False)
    old_target = _retain(root, "OLD")
    identity = published_scope(root).knowledge.identity
    store = KnowledgeStore(root, identity)
    old_files = {**_files(root, old_target, "OLD"), "concepts/obsolete.md": b"Retired page"}
    first = store.publish(
        store.prepare_revision(
            old_files,
            expected=store.bind().publication_id,
            unit_revisions=(old_target.unit_revision_id,),
        )
    )
    assert ProjectionStore(root, identity).sync().status == "synced"
    target = _retain(root, "NEW")
    spec, evidence = tmp_path / "spec.json", tmp_path / "evidence.json"
    spec.write_text(
        json.dumps(
            {
                "identity": asdict(identity),
                "target": target.unit_revision_id,
                "old_publication": first.publication_id,
            }
        )
    )
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__file__).parent.resolve()), str(Path(__file__).parent.parent.resolve())]
        ),
    }
    program = (
        "from pathlib import Path; import sys; "
        "from test_unit_publication_process_recovery import _crash_publication; "
        "_crash_publication(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3]),"
        "sys.argv[4],sys.argv[5])"
    )
    child = subprocess.run(
        [sys.executable, "-c", program, str(root), str(spec), str(evidence), point, timing],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == EXIT_CODE, child.stderr
    observed = store.bind()
    expected = (
        "OLD" if point == "blob" or (point == "publication" and timing == "before") else "NEW"
    )
    assert observed.read("index.md") == f"{expected} index".encode()
    selected = old_target if expected == "OLD" else target
    for path, payload in _files(root, selected, expected).items():
        assert observed.read(path) == payload
    assert observed.unit_revisions() == (selected.unit_revision_id,)
    assert observed.read_original(selected.unit_revision_id).startswith(expected.encode())
    assert store.receipt(observed.publication_id).revision_id == observed.publication.revision_id
    retained = json.loads(evidence.read_text())
    work = CompileWork(
        root,
        identity,
        retained["work_id"],
        retained["budget_id"],
        retained["object_digest"],
        retained["execution_id"],
    )
    assert (
        work.load("summary:1", digest("accepted-input"))["content"] == "Accepted before publication"
    )
    assert (
        IndexStore(root, identity).document(retained["index_revision_id"])["pages"][0]["content"]
        == "NEW index content"
    )
    assert ProjectionStore(root, identity).sync().status == "synced"
    for path, payload in _files(root, selected, expected).items():
        assert (root / "wiki" / path).read_bytes() == payload
    if expected == "NEW":
        assert not (root / "wiki/concepts/obsolete.md").exists()
