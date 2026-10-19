"""An accepted native checkpoint survives process exit and publication conflict."""

import subprocess
import sys
import textwrap

import pytest

from ultrakb.compile_checkpoints import digest
from ultrakb.storage.knowledge_store import KnowledgeStore, PublicationConflict


@pytest.mark.parametrize("exit_code", [0, 42])
def test_checkpoint_survives_process_exit_and_publication_conflict(compile_work, exit_code):
    work = compile_work({"document": "retained-input"}, {"protocol": "current"})
    store = KnowledgeStore(work.root, work.identity)
    base = store.bind()
    candidate = store.prepare_changes(base, {"concepts/unpublished.md": b"Unpublished draft"})
    competing = store.publish(store.prepare_changes(base, {"concepts/human.md": b"Human content"}))
    program = textwrap.dedent("""\
        import os
        import sys
        from pathlib import Path
        from ultrakb.compile_checkpoints import digest
        from ultrakb.storage.checkpoint_store import CompileWork
        from ultrakb.storage.database import DatabaseIdentity

        root, kb_id, generation, work_id, budget, object_digest, execution, exit_code = sys.argv[1:]
        work = CompileWork(Path(root), DatabaseIdentity(kb_id, generation), work_id,
                           budget, object_digest, execution)
        work.save("summary:1", digest("request"), {"content": "Accepted computation"})
        os._exit(int(exit_code))
    """)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(work.root),
            work.identity.kb_id,
            work.identity.generation,
            work.work_id,
            work.model_budget_id,
            work.object_digest,
            work.execution_id,
            str(exit_code),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == exit_code, child.stderr
    with pytest.raises(PublicationConflict):
        store.publish(candidate)
    reopened = compile_work({"document": "retained-input"}, {"protocol": "current"})
    assert reopened.load("summary:1", digest("request")) == {"content": "Accepted computation"}
    assert store.bind().publication_id == competing.publication_id
    assert store.bind().read("concepts/human.md") == b"Human content"
    with pytest.raises(FileNotFoundError):
        store.bind().read("concepts/unpublished.md")
