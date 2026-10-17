"""Generated Markdown is prose; only explicit references promise transcription."""

import json

import pytest

from ultrakb.agent.page_delivery import validate_page_delivery
from ultrakb.agent.source_excerpts import SourceExcerpts


@pytest.mark.parametrize("path", ["/root/data_restore/", "/var/log/pods/"])
@pytest.mark.parametrize("layout", ["{}", "`{}`", "```text\n{}\n```", "| Path |\n| --- |\n| {} |"])
def test_generated_paths_are_accepted_in_every_markdown_layout(path, layout):
    source = "备份保存在/root/data_restore/下。日志存放在/var/log/pods/目录。"
    content = layout.format(path)
    raw = json.dumps({"description": f"检查 `{path}`。", "content": content})
    delivered = json.loads(validate_page_delivery(raw, original_excerpts=SourceExcerpts([source])))
    assert delivered["content"] == content
    assert delivered["description"] == f"检查 `{path}`。"


@pytest.mark.asyncio
async def test_generated_examples_and_calculations_do_not_request_model_repair(monkeypatch):
    from ultrakb.agent.compiler import _llm_call_page_async

    async def no_model_send(*args, **kwargs):
        pytest.fail("Ordinary generated content must not request repair")

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call_async", no_model_send)
    source = "| object | condition | delay |\n| --- | --- | --- |\n| A | active | 12 ms |"
    content = (
        "| object | condition | delay |\n| --- | --- | --- |\n| A | active | 99 ms |\n\n"
        '7 * 5 = 36\n\n```json\n{"generated_example": 42}\n```\n\n'
        "推导模板：`/new/{name}/{id}`。"
    )
    raw = json.dumps({"description": "Example", "content": content})
    result = await _llm_call_page_async(
        "unused", [], "acceptance", initial_response=raw, original_sources=[source]
    )
    assert json.loads(result)["content"] == content


@pytest.mark.asyncio
async def test_saved_entity_is_revalidated_before_exhausted_repair_gate(kb_dir, monkeypatch):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.create_generation import prepare_page_creation

    options, _, checkpoints = creation_inputs(kb_dir, kind="entities", limit=1000000)
    prepared = prepare_page_creation(**options)
    [batch] = prepared.batches
    raw = json.dumps(
        {"description": "Restore", "type": "other", "content": "使用 `/root/data_restore/`。"}
    )
    checkpoints.claim_repair(batch.key, batch.signature)
    checkpoints.record_invalid_response(
        batch.key,
        batch.signature,
        raw,
        stage="entity: rules",
        response_protocol="page-json",
        expected_sections=(),
        code="page_literal_conflict",
        detail="Historical validator",
        attempt=2,
    )

    async def no_model_send(*args, **kwargs):
        pytest.fail("A reusable saved entity needs no model send")

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call_async", no_model_send)
    result = await prepare_page_creation(**options).generate()
    assert result["content"] == "使用 `/root/data_restore/`。"
    assert checkpoints.load(batch.key, batch.signature)["content"] == result["content"]
    entry = json.loads(checkpoints.path.read_text())["entries"][batch.key]
    assert entry["repair_used"] is True
    assert len(entry["response_failures"]) == 1
    assert entry["reused_response"]["response_sha256"]


@pytest.mark.asyncio
async def test_saved_update_recovers_with_spent_budget_and_preserves_page_base(kb_dir, monkeypatch):
    from publication_inputs import update_inputs

    from ultrakb.agent.update_generation import generate_page_update
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.compile_checkpoints import digest, document_checkpoints
    from ultrakb.locks import kb_ingest_lock

    old = "# Rules\n\nOld recovery rule.\n"
    page, reader, originals, patch, _, messages = update_inputs(
        kb_dir, old, "日志存放在/var/log/pods/目录。"
    )

    async def no_model_send(*args, **kwargs):
        pytest.fail("A reusable saved update needs no model send")

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call_async", no_model_send)
    context = ExecutionContext()
    with kb_ingest_lock(kb_dir / ".ultrakb"), context.begin(kb_dir):
        cp = document_checkpoints(kb_dir, "legacy", "manual")
        key = f"page:concepts/rules.md:{patch.digest}:batch:0"
        signature = digest(messages(list(originals.windows), patch))
        raw = json.dumps(
            {
                "status": "updated",
                "base_sha256": patch.digest,
                "edits": [
                    {
                        "block_id": "b2",
                        "operation": "replace",
                        "content": "检查 `/var/log/pods/`。\n",
                    }
                ],
            }
        )
        cp.claim_repair(key, signature)
        cp.record_invalid_response(
            key,
            signature,
            raw,
            stage=key,
            response_protocol="page-json",
            expected_sections=(),
            code="page_literal_conflict",
            detail="Historical path rejection",
            attempt=2,
        )
        context.executor.budget.restore(1000, {"compile": 1000})
        before = context.model_task.record.model_dump()
        result = await generate_page_update(
            patch=patch,
            originals=originals,
            windows=reader,
            page_path=page,
            model="unused",
            bundle=None,
            system_message={
                "role": "system",
                "content": "Compile source facts into the wiki page.",
            },
            current_original=None,
            known_targets_message={"role": "user", "content": "rules"},
            task_message={"role": "user", "content": "Update all required conditions."},
            fits=lambda _: True,
        )
        assert "检查 `/var/log/pods/`。" in result["content"]
        assert cp.load(key, signature)["edits"]
        assert context.model_task.record.model_dump() == before
    assert page.read_text() == old


def test_unreconstructable_historical_request_keeps_its_response_and_fails(kb_dir):
    from test_create_capacity import creation_inputs

    from ultrakb.agent.create_generation import prepare_page_creation
    from ultrakb.agent.page_response import PageResponseError

    options, _, cp = creation_inputs(kb_dir, limit=1000000)
    [batch] = prepare_page_creation(**options).batches
    cp.claim_repair(batch.key, "historical-request-cannot-be-reconstructed")
    before = cp.path.read_bytes()
    with pytest.raises(PageResponseError, match="compile_request_changed"):
        prepare_page_creation(**options)
    assert cp.path.read_bytes() == before


@pytest.mark.parametrize("template", ["/api/{role}/new", "arbitrary generated example"])
def test_structured_prose_and_compatible_templates_keep_only_explicit_reference_checks(template):
    excerpts = SourceExcerpts(["Original source conditions."])
    prose = "```text\n[[original:x99.99]]\nnew_field=42\n```"
    raw = json.dumps(
        {
            "description": "Guide",
            "sections": [
                {
                    "id": "s1",
                    "parts": [
                        {"kind": "prose", "text": prose, "refs": []},
                        {"kind": "derived_template", "text": template, "refs": ["x1.1"]},
                    ],
                }
            ],
        }
    )
    delivered = json.loads(
        validate_page_delivery(
            raw,
            response_protocol="summary-parts-json",
            expected_sections=("s1",),
            original_excerpts=excerpts,
        )
    )
    assert prose in delivered["sections"][0]["content"]
    assert template in delivered["sections"][0]["content"]
    assert "Original source conditions." in delivered["sections"][0]["content"]
    assert excerpts.resolutions[0]["references"][0]["source_number"] == 1


def test_legacy_task_children_inherit_request_bytes_and_paid_counts(kb_dir):
    from ultrakb.config import LlmCredentialBundle
    from ultrakb.llm_execution import CompletionExecutor, ModelCallPolicy, RoleBindings
    from ultrakb.locks import atomic_write_json
    from ultrakb.model_task import TaskJournal, read_task, task_path

    bindings = RoleBindings("unused", LlmCredentialBundle())
    first = CompletionExecutor(bindings, ModelCallPolicy(max_calls=5, retries=0))
    first.budget.restore(2, {"compile": 2})
    journal = TaskJournal(kb_dir, first)
    assert journal.record.compiler_request_version == 3
    path = task_path(kb_dir, journal.record.task_id)
    legacy = json.loads(path.read_text())
    legacy.pop("compiler_request_version")
    atomic_write_json(path, legacy)
    before = path.read_bytes()
    assert read_task(kb_dir, journal.record.task_id).compiler_request_version == 1
    extended = CompletionExecutor(bindings, ModelCallPolicy(max_calls=6, retries=0))
    child = TaskJournal(kb_dir, extended, resume=journal.record.task_id)
    assert child.record.parent_task_id == journal.record.task_id
    assert child.record.compiler_request_version == 1
    assert child.record.used == extended.calls == 2
    assert child.record.stages == {"compile": 2}
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_update_reserves_protocol_repair_capacity_before_any_send(kb_dir, monkeypatch):
    from publication_inputs import update_inputs

    from ultrakb.agent.page_response import PageResponseError
    from ultrakb.agent.update_generation import generate_page_update

    page, reader, originals, patch, _, _ = update_inputs(
        kb_dir, "# Rules\n\nKeep human content.\n", "Required condition."
    )

    async def no_model_send(*args, **kwargs):
        pytest.fail("An indivisible update must reserve room for its protocol repair")

    monkeypatch.setattr("ultrakb.agent.compiler._llm_call_async", no_model_send)

    def fits(messages):
        return not any(
            str(message.get("content", "")).startswith("The page response failed validation")
            for message in messages
        )

    with pytest.raises(PageResponseError, match="page_update_capacity_exceeded"):
        await generate_page_update(
            patch=patch,
            originals=originals,
            windows=reader,
            page_path=page,
            model="unused",
            bundle=None,
            system_message={"role": "system", "content": "Compile"},
            current_original=None,
            known_targets_message={"role": "user", "content": "rules"},
            task_message={"role": "user", "content": "Update"},
            fits=fits,
        )
    assert page.read_text() == "# Rules\n\nKeep human content.\n"


@pytest.mark.parametrize("extra", [None, [42], [{"kind": "prose", "text": 5, "refs": []}]])
def test_page_metadata_does_not_enter_the_structured_summary_protocol(extra):
    raw = json.dumps({"description": "Guide", "content": "Ordinary page.", "parts": extra})
    delivered = json.loads(
        validate_page_delivery(raw, original_excerpts=SourceExcerpts(["Original"]))
    )
    assert delivered["content"] == "Ordinary page."
