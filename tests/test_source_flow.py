"""Source flow uses durable outcomes and the correct live processing item."""

from dataclasses import replace

import pytest

from openkb.desktop.source_flow_state import flow_steps
from openkb.progress import ProgressStep
from openkb.runtime.records import TaskView, UnitResult
from openkb.runtime.requests import ContinueSource, ImportFile
from openkb.runtime.source_activity import SourceActivity, find_source_activity


def saved(stage, state="unfinished", reason=None):
    return {
        "source": {"name": "manual.docx"},
        "result": {
            "stage": stage,
            "knowledge_compilation": state,
            "reason": reason,
            "parse_id": "a" * 64 if stage != "source_intake" else None,
        },
    }


@pytest.mark.parametrize(
    "phase,current,done",
    [
        ("source_intake", "parsing", 1),
        ("parsing", "parsing", 1),
        ("parsed", "facts", 2),
        ("facts", "facts", 2),
        ("planning", "planning", 3),
        ("generation", "generation", 4),
        ("committing", "publication", 5),
    ],
)
def test_flow_marks_only_recorded_preceding_stages_complete(phase, current, done):
    rows = flow_steps(saved(phase, "not_started" if phase == "source_intake" else "unfinished"))
    assert next(row.key for row in rows if row.current) == current
    assert sum(row.state == "completed" for row in rows) == done
    assert rows[-1].state != "completed"


@pytest.mark.parametrize(
    "phase,reason", [("parsing", "source_quality_needs_review"), ("committing", "needs_acceptance")]
)
def test_review_is_at_the_blocking_stage(phase, reason):
    rows = flow_steps(saved(phase, reason=reason))
    assert next(row.state for row in rows if row.current) == "review"


def test_unknown_compilation_position_is_not_guessed():
    rows = flow_steps(saved("compiling"))
    assert not any(row.current for row in rows)
    assert [row.state for row in rows[2:5]] == ["unknown"] * 3


def test_live_retry_replaces_old_failure_and_does_not_publish_at_100_percent():
    activity = SourceActivity("b" * 32, "running", "facts", (ProgressStep("facts", 100, 100),))
    rows = flow_steps(saved("planning", reason="request_timeout"), activity)
    assert rows[2].state == "running" and rows[2].progress.startswith("100%")
    assert rows[3].state == rows[-1].state == "pending"


def test_completed_and_auxiliary_navigation_do_not_become_incomplete():
    assert all(row.state == "completed" for row in flow_steps(saved("committed", "completed")))
    activity = SourceActivity("b" * 32, "running", "navigation", ())
    assert all(
        row.state == "completed" for row in flow_steps(saved("committed", "completed"), activity)
    )


def test_reparsing_a_completed_source_does_not_claim_new_compilation_complete():
    activity = SourceActivity("b" * 32, "running", "parsing", ())
    rows = flow_steps(saved("committed", "completed"), activity)
    assert rows[1].state == "running" and rows[-1].state == "pending"


def test_activity_distinguishes_versions_knowledge_bases_and_later_batch_items(tmp_path):
    root = str(tmp_path.resolve())
    one, two, version = "1" * 32, "2" * 32, "a" * 64
    task = TaskView("b" * 32, root, "ContinueSource", "running", "generation", 2, (), False, False)
    requests = (ContinueSource(one, version), ContinueSource(two, version))
    snapshots = [(task, requests)]
    assert find_source_activity(snapshots, tmp_path, one, version).stage == "generation"
    later = find_source_activity(snapshots, tmp_path, two, version)
    assert later.stage == later.state == "queued" and not later.progress
    assert find_source_activity(snapshots, tmp_path / "other", one, version) is None
    assert find_source_activity(snapshots, tmp_path, one, "c" * 64) is None
    finished = replace(task, results=(UnitResult("completed"),))
    assert find_source_activity([(finished, requests)], tmp_path, one, version) is None
    assert (
        find_source_activity([(replace(task, state="completed"), requests)], tmp_path, one, version)
        is None
    )


def test_import_matching_uses_origin_not_display_name(tmp_path):
    source = tmp_path.parent / "manual.docx"
    task = TaskView("b" * 32, str(tmp_path), "ImportFile", "running", "facts", 1, (), False, False)
    snapshots = [(task, (ImportFile(str(source)),))]
    import os

    origin = "file:" + os.path.normcase(source.as_posix())
    assert find_source_activity(snapshots, tmp_path, "1" * 32, "a" * 64, origin)
    assert (
        find_source_activity(snapshots, tmp_path, "1" * 32, "a" * 64, "file:/other/manual.docx")
        is None
    )


def test_running_item_takes_precedence_over_a_newer_queued_retry(tmp_path):
    one, version = "1" * 32, "a" * 64
    request = (ContinueSource(one, version),)
    active = TaskView(
        "b" * 32, str(tmp_path), "ContinueSource", "running", "facts", 1, (), False, False
    )
    queued = replace(active, id="c" * 32, state="queued", stage="queued")
    value = find_source_activity([(active, request), (queued, request)], tmp_path, one, version)
    assert value.task_id == active.id and value.stage == "facts"


def test_safe_stop_retains_last_measured_phase():
    activity = SourceActivity(
        "b" * 32, "stopping", "stopping", (ProgressStep("planning", 128, 300),)
    )
    rows = flow_steps(saved("facts"), activity)
    assert rows[3].state == "stopping" and rows[3].current
