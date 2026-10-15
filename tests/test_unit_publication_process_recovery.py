"""Publication survives process death; candidate content is explicit domain input.

No model, compiler result or network response is substituted. The child performs
the actual publication and exits without running Python exception/finally cleanup.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ultrakb.application.ingestion import result_from_publication
from ultrakb.application.refresh import list_knowledge_history
from ultrakb.application.views import view_scope
from ultrakb.ingest_records import ImportUnit, KnowledgeRevision, UnitPublication, UnitRevision
from ultrakb.inputs import prepared_input
from ultrakb.locks import (
    atomic_write_json,
    atomic_write_text,
    durable_unlink,
    kb_ingest_lock,
    kb_read_lock,
)
from ultrakb.source_catalog import (
    admit_source_revision,
    read_admission,
    read_record,
    read_source,
    read_source_revision,
)
from ultrakb.unit_publication import (
    begin_unit_attempt,
    plan_import_units,
    prepare_compile_view,
    publish_unit_revision,
    read_head,
    read_unit_publication,
)

OLD_SOURCE = "The inlet remains closed until inspection.\n"
NEW_SOURCE = "The inlet remains closed until the winter inspection.\n"
EXIT_CODE = 73


def _forbid_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
        raise AssertionError("Publication recovery uses only local domain inputs")


def _retain_target(kb, body):
    path = kb / "manual.txt"
    atomic_write_text(path, body)
    with prepared_input(path) as prepared:
        admission = admit_source_revision(kb, prepared)
    unit, revision = plan_import_units(kb, admission, "publication-process-recovery-v1")
    state, runnable = begin_unit_attempt(kb, unit, revision)
    assert runnable
    return admission, unit, revision, state


def _candidate_pages(wiki, doc_name, body, *, replacement):
    atomic_write_text(wiki / f"sources/{doc_name}.md", body)
    atomic_write_text(
        wiki / f"summaries/{doc_name}.md",
        f"---\ntype: Summary\ndescription: Retained inlet instructions\n"
        f"full_text: sources/{doc_name}.md\n---\n\n{body}",
    )
    if replacement:
        durable_unlink(wiki / "sources/retired-excerpt.md", missing_ok=True)
        atomic_write_text(wiki / "concepts/winter.md", "# Winter inspection\n\nNew inlet rule.\n")
    else:
        atomic_write_text(wiki / "sources/retired-excerpt.md", body)
        atomic_write_text(wiki / "concepts/retired.md", "# Inspection\n\nPrevious inlet rule.\n")


def _seed_and_prepare(kb):
    with kb_ingest_lock(kb / ".ultrakb"):
        admission, unit, revision, state = _retain_target(kb, OLD_SOURCE)
        with prepare_compile_view(kb) as view:
            _candidate_pages(view.scope.wiki_dir, unit.doc_name, OLD_SOURCE, replacement=False)
            first = publish_unit_revision(
                kb,
                admission,
                unit,
                revision,
                state,
                view,
                normalized_source=f"sources/{unit.doc_name}.md",
                normalized_format="markdown",
                is_long=False,
            )
        assert first.status == "completed"
        target = _retain_target(kb, NEW_SOURCE)
        before = read_head(kb)
        old_pages = {
            p.relative_to(kb / "wiki").as_posix(): p.read_bytes()
            for p in (kb / "wiki").rglob("*")
            if p.is_file()
        }
    return first, target, before, old_pages


def _crash_publication(kb, source_id, point, timing, evidence):
    """Only the child installs delegating storage observers used to terminate it."""
    from ultrakb import unit_publication

    sys.addaudithook(_forbid_network)
    with kb_ingest_lock(kb / ".ultrakb"):
        admission = read_admission(kb, read_source(kb, source_id))
        unit, revision = plan_import_units(kb, admission, "publication-process-recovery-v1")
        state = read_unit_publication(kb, unit.unit_id)
        with prepare_compile_view(kb) as view:
            _candidate_pages(view.scope.wiki_dir, unit.doc_name, NEW_SOURCE, replacement=True)
            copy_tree = unit_publication.copy_tree
            copy_file = unit_publication._copy_file_atomic
            write_record = unit_publication.write_record
            unlink = unit_publication.durable_unlink
            from ultrakb.mutation import MutationSnapshot

            mark_committed = MutationSnapshot.mark_committed
            candidate_id = None

            def stop_at(boundary, phase):
                if (boundary, phase) == (point, timing):
                    assert candidate_id is not None
                    atomic_write_json(
                        evidence,
                        {
                            "point": point,
                            "timing": timing,
                            "knowledge_revision_id": candidate_id,
                            "attempt_id": state.attempt_id,
                        },
                    )
                    os._exit(EXIT_CODE)

            def observed_copy(source, destination):
                nonlocal candidate_id
                boundary = None
                if destination.parent.parent.name == "revisions":
                    candidate_id = destination.parent.name
                    boundary = "immutable_copy"
                elif destination == kb / "wiki":
                    boundary = "live_copy"
                stop_at(boundary, "before")
                copy_tree(source, destination)
                stop_at(boundary, "after")

            def observed_record(path, record):
                boundary = None
                if path.parent.parent.name == "revisions":
                    boundary = {"manifest.json": "manifest", "publication.json": "publication"}.get(
                        path.name
                    )
                elif path.name == "head.json":
                    boundary = "head"
                elif path.parent.name == "publications":
                    boundary = "state"
                elif path.parent.name == "attempts":
                    boundary = "attempt"
                stop_at(boundary, "before")
                write_record(path, record)
                stop_at(boundary, "after")

            def observed_file(source, destination):
                copy_file(source, destination)
                if candidate_id is not None:
                    immutable = kb / ".ultrakb/knowledge/legacy/revisions" / candidate_id / "wiki"
                    if destination.is_relative_to(immutable):
                        stop_at("partial_immutable_copy", "after")
                if destination == kb / "wiki/concepts/winter.md":
                    stop_at("partial_live_copy", "after")

            def observed_unlink(path, **kwargs):
                boundary = "live_delete" if path == kb / "wiki/sources/retired-excerpt.md" else None
                stop_at(boundary, "before")
                unlink(path, **kwargs)
                stop_at(boundary, "after")

            def observed_commit(snapshot):
                if snapshot.operation == "publish-unit-revision":
                    stop_at("journal_commit", "before")
                mark_committed(snapshot)
                if snapshot.operation == "publish-unit-revision":
                    stop_at("journal_commit", "after")

            unit_publication.copy_tree = observed_copy
            unit_publication._copy_file_atomic = observed_file
            unit_publication.write_record = observed_record
            unit_publication.durable_unlink = observed_unlink
            MutationSnapshot.mark_committed = observed_commit
            publish_unit_revision(
                kb,
                admission,
                unit,
                revision,
                state,
                view,
                normalized_source=f"sources/{unit.doc_name}.md",
                normalized_format="markdown",
                is_long=False,
            )
    raise AssertionError("The requested storage boundary was not reached")


def _child_environment(project):
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(part in key.upper() for part in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    env["PYTHONPATH"] = os.pathsep.join((str(project), str(project / "tests")))
    return env


def _read_after_restart(kb, unit_id, evidence):
    """A fresh interpreter takes the first normal lease and observes restored state."""
    sys.addaudithook(_forbid_network)
    with kb_read_lock(kb / ".ultrakb"):
        head = read_head(kb)
        state = read_unit_publication(kb, unit_id)
        atomic_write_json(
            evidence,
            {
                "head_revision_id": head.knowledge_revision_id,
                "generation": head.generation,
                "unit_id": state.unit_id,
                "target_revision_id": state.target_revision_id,
                "successful_revision_id": state.successful_revision_id,
                "publication_revision_id": state.knowledge_revision_id,
                "attempt_id": state.attempt_id,
                "status": state.status,
            },
        )


def _run_restarted_reader(kb, unit_id, evidence):
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from test_unit_publication_process_recovery import _read_after_restart; "
            "_read_after_restart(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))",
            str(kb),
            unit_id,
            str(evidence),
        ],
        cwd=project,
        env=_child_environment(project),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(evidence.read_text())


def _run_crashed_child(kb, source_id, point, timing, evidence):
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from test_unit_publication_process_recovery import _crash_publication; "
            "_crash_publication(Path(sys.argv[1]), sys.argv[2], sys.argv[3], "
            "sys.argv[4], Path(sys.argv[5]))",
            str(kb),
            source_id,
            point,
            timing,
            str(evidence),
        ],
        cwd=project,
        env=_child_environment(project),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == EXIT_CODE, result.stdout + result.stderr
    return json.loads(evidence.read_text())


@pytest.mark.parametrize(
    ("point", "timing"),
    [
        (point, timing)
        for point in (
            "immutable_copy",
            "manifest",
            "publication",
            "live_copy",
            "live_delete",
            "head",
            "state",
            "attempt",
            "journal_commit",
        )
        for timing in ("before", "after")
    ]
    + [
        ("partial_immutable_copy", "after"),
        ("partial_live_copy", "after"),
    ],
)
def test_process_death_exposes_one_complete_publication(kb_dir, tmp_path, point, timing):
    first, target, before, old_pages = _seed_and_prepare(kb_dir)
    admission, unit, revision, started = target
    observed = _run_crashed_child(
        kb_dir, admission.source.source_id, point, timing, tmp_path / "child-exit.json"
    )
    assert observed["attempt_id"] == started.attempt_id
    assert (observed["point"], observed["timing"]) == (point, timing)
    committed = (point, timing) == ("journal_commit", "after")
    journals = list((kb_dir / ".ultrakb/journal").glob("*.json"))
    assert len(journals) == 1
    assert json.loads(journals[0].read_text())["status"] == ("committed" if committed else "active")
    if point == "partial_live_copy":
        assert (kb_dir / "wiki/concepts/winter.md").exists()
        assert (kb_dir / f"wiki/sources/{unit.doc_name}.md").read_text() == OLD_SOURCE

    restarted = _run_restarted_reader(kb_dir, unit.unit_id, tmp_path / "restarted-read.json")
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))
    with kb_read_lock(kb_dir / ".ultrakb"):
        head = read_head(kb_dir)
        pages = {
            p.relative_to(kb_dir / "wiki").as_posix(): p.read_bytes()
            for p in (kb_dir / "wiki").rglob("*")
            if p.is_file()
        }
        current = read_unit_publication(kb_dir, unit.unit_id)
        assert restarted == {
            "head_revision_id": head.knowledge_revision_id,
            "generation": head.generation,
            "unit_id": current.unit_id,
            "target_revision_id": current.target_revision_id,
            "successful_revision_id": current.successful_revision_id,
            "publication_revision_id": current.knowledge_revision_id,
            "attempt_id": current.attempt_id,
            "status": current.status,
        }
        if committed:
            assert current.status == "completed" and current.stage == "published"
            assert current.successful_revision_id == revision.unit_revision_id
            assert current.knowledge_revision_id == observed["knowledge_revision_id"]
            assert current.attempt_id == started.attempt_id and current.job_id == revision.job_id
            assert current.proposal_id is None and current.error_type is None
            assert head.generation == before.generation + 1
            assert head.inputs == {**before.inputs, unit.unit_id: revision.unit_revision_id}
            assert pages[f"sources/{unit.doc_name}.md"] == NEW_SOURCE.encode()
            assert NEW_SOURCE.encode() in pages[f"summaries/{unit.doc_name}.md"]
            assert "sources/retired-excerpt.md" not in pages
            # Pending old knowledge is retained by the real publication policy.
            assert pages["concepts/retired.md"] == old_pages["concepts/retired.md"]
            assert pages["concepts/winter.md"] == b"# Winter inspection\n\nNew inlet rule.\n"
        else:
            assert head == before and pages == old_pages
            assert current == started
        assert head.knowledge_revision_id == current.knowledge_revision_id
        assert head.inputs[unit.unit_id] == current.successful_revision_id
        assert read_record(kb_dir, "attempts", started.attempt_id, UnitPublication) == current
        assert read_record(kb_dir, "attempts", first.attempt_id, UnitPublication) == first
        receipt = result_from_publication(
            kb_dir, admission, current, status="added" if committed else "blocked"
        )
        assert receipt.units[0].target_revision_id == revision.unit_revision_id
        assert receipt.units[0].successful_revision_id == current.successful_revision_id
        assert receipt.units[0].knowledge_revision_id == current.knowledge_revision_id
        old_input = read_record(
            kb_dir, "unit-revisions", first.successful_revision_id, UnitRevision
        )
        old_original = read_source_revision(kb_dir, old_input.source_revision_id)
        assert (kb_dir / old_original.original).read_text() == OLD_SOURCE
        assert receipt.units[0].successful_source_revision_id == (
            admission.revision.source_revision_id if committed else old_input.source_revision_id
        )
        assert all(Path(path).exists() for path in receipt.resources)
        directory = kb_dir / ".ultrakb/knowledge/legacy/revisions"
        if committed:
            published = directory / observed["knowledge_revision_id"]
            assert (
                UnitPublication.model_validate_json((published / "publication.json").read_bytes())
                == current
            )
            manifest = KnowledgeRevision.model_validate_json(
                (published / "manifest.json").read_bytes()
            )
            assert manifest.base_revision_id == first.knowledge_revision_id
            assert manifest.unit_revision_id == current.successful_revision_id
            assert manifest.generated_baselines == head.generated_baselines
            retained = {
                p.relative_to(published / "wiki").as_posix(): p.read_bytes()
                for p in (published / "wiki").rglob("*")
                if p.is_file()
            }
            assert retained == pages
        else:
            assert not (directory / observed["knowledge_revision_id"]).exists()
        manifest = KnowledgeRevision.model_validate_json(
            (directory / first.knowledge_revision_id / "manifest.json").read_bytes()
        )
        assert manifest.unit_revision_id == first.successful_revision_id
        assert (
            UnitPublication.model_validate_json(
                (directory / first.knowledge_revision_id / "publication.json").read_bytes()
            )
            == first
        )
        assert read_record(kb_dir, "units", unit.unit_id, ImportUnit) == unit
        original = read_source_revision(kb_dir, admission.revision.source_revision_id)
        assert (kb_dir / original.original).read_text() == NEW_SOURCE
    history = list_knowledge_history(kb_dir, scope=view_scope(kb_dir, "legacy"))
    assert [item["knowledge_revision_id"] for item in history] == (
        [current.knowledge_revision_id, first.knowledge_revision_id]
        if committed
        else [first.knowledge_revision_id]
    )
    historical = view_scope(kb_dir, "legacy", historical_revision=first.knowledge_revision_id)
    assert (historical.wiki_dir / f"sources/{unit.doc_name}.md").read_text() == OLD_SOURCE
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))
    with kb_read_lock(kb_dir / ".ultrakb"):
        assert read_head(kb_dir) == head  # Ordinary recovery is idempotent.
        assert read_unit_publication(kb_dir, unit.unit_id) == current
    with kb_ingest_lock(kb_dir / ".ultrakb"):
        retry, runnable = begin_unit_attempt(kb_dir, unit, revision)
        assert not runnable  # A completed or unconfirmed attempt is not silently replayed.
        assert retry.attempt_id == started.attempt_id
        assert retry.successful_revision_id == current.successful_revision_id
        assert retry.knowledge_revision_id == current.knowledge_revision_id
        assert retry.status == ("completed" if committed else "interrupted")
        assert read_record(kb_dir, "attempts", retry.attempt_id, UnitPublication) == retry
