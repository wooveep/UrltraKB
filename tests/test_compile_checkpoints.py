"""Native accepted computation survives storage failures and changed input guards."""

import sqlite3

import pytest


def accepted_summary_input(window):
    from ultrakb.agent.document_summary import window_sections

    return {
        "description": "Accepted operating condition",
        "sections": [
            {"id": section["id"], "content": window["content"][section["start"] : section["end"]]}
            for section in window_sections(window)
        ],
    }


@pytest.mark.parametrize("used,admitted", [(3, True), (4, False)])
def test_one_missing_summary_and_plan_require_two_remaining_sends(used, admitted):
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.execution_budget import admit_document_summary
    from ultrakb.llm_execution import (
        CompletionExecutor,
        ModelBudgetExceeded,
        ModelCallPolicy,
        RoleBindings,
    )

    executor = CompletionExecutor(
        RoleBindings("deepseek/deepseek-flash", LlmCredentialBundle()),
        ModelCallPolicy(max_calls=5, retries=0),
    )
    executor.budget.restore(used, {"compile": used})
    with executor.activate():
        if admitted:
            admit_document_summary(1)
            executor.budget.require("compile", 2)
        else:
            with pytest.raises(ModelBudgetExceeded, match="compile needs at least 2 sends"):
                admit_document_summary(1)
    assert executor.calls == used and executor.budget.snapshot()["used"] == used


@pytest.mark.parametrize("boundary", ["blob", "reference"])
def test_incomplete_checkpoint_write_is_not_accepted(compile_work, monkeypatch, boundary):
    import ultrakb.storage.checkpoint_store as storage
    from ultrakb.compile_checkpoints import digest
    from ultrakb.storage.coordination import storage_stage

    checkpoint = compile_work({"document": "manual"}, {"protocol": "current"})
    signature = digest("request")
    if boundary == "reference":
        with storage_stage(checkpoint.root, checkpoint.identity) as db, db.transaction() as sql:
            sql.execute(
                "CREATE TRIGGER fail_checkpoint BEFORE INSERT ON ukb_compile_steps "
                "BEGIN SELECT RAISE(ABORT, 'Injected storage failure'); END"
            )
    with monkeypatch.context() as patch:
        if boundary == "blob":

            def fail(*args, **kwargs):
                raise OSError("Injected storage failure")

            patch.setattr(storage, "put_blob", fail)
        with pytest.raises((OSError, sqlite3.Error), match="Injected"):
            checkpoint.save("summary:1", signature, {"content": "Accepted"})
    assert checkpoint.load("summary:1", signature) is None
    if boundary == "reference":
        with storage_stage(checkpoint.root, checkpoint.identity) as db, db.transaction() as sql:
            sql.execute("DROP TRIGGER fail_checkpoint")
    checkpoint.save("summary:1", signature, {"content": "Accepted"})
    reopened = compile_work({"document": "manual"}, {"protocol": "current"})
    assert reopened.load("summary:1", signature) == {"content": "Accepted"}


def test_changed_conditions_and_page_base_cannot_reuse_accepted_payload(compile_work):
    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.compile_checkpoints import digest

    first = compile_work({"document": "manual"}, {"protocol": "current"})
    signature = digest("request")
    first.save("summary:1", signature, {"content": "Accepted"})
    changed = compile_work({"document": "manual"}, {"protocol": "next"})
    assert changed.load("summary:1", signature) is None
    assert first.load("summary:1", digest("changed-input")) is None
    first.bind_page("concepts/device.md", digest("old-base"))
    restarted = compile_work({"document": "manual"}, {"protocol": "current"})
    with pytest.raises(PageResponseError, match="page_update_stale"):
        restarted.bind_page("concepts/device.md", digest("new-base"))
    first.claim_conflict("conflict", signature)
    first.claim_repair("conflict", signature)
    with pytest.raises(PageResponseError, match="compile_repair_exhausted"):
        restarted.require_generation("conflict", signature)
    with pytest.raises(PageResponseError, match="compile_conflict_exhausted"):
        restarted.claim_conflict("conflict", signature)
