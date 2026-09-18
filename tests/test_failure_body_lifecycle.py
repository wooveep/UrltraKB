"""Content omissions retain their reason, never a completed batch's traceback bodies."""

import gc
import json
import weakref

import litellm
import pytest
import yaml

from openkb.application.documents import import_document
from tests.http_model_fixture import evidence_response
from tests.test_adaptive_processing import response


@pytest.mark.parametrize("stage", ["facts", "planning", "generation"])
def test_failed_batch_bodies_are_released_before_later_batches(
    kb_dir, tmp_path, monkeypatch, stage
):
    from openkb.agent import evidence_facts, evidence_pages, evidence_plan
    from openkb.agent.evidence_retry import ResponseIncomplete, retry_batches

    settings_path = kb_dir / ".openkb/config.yaml"
    settings = yaml.safe_load(settings_path.read_text())
    settings["processing"].update(concurrency=1, max_attempts=1)
    settings_path.write_text(yaml.safe_dump(settings))
    source = tmp_path / "omissions.md"
    source.write_text("\n\n".join(f"# Operation {n}\n\nKeep backup {n} intact." for n in range(4)))
    references, retained = [], []

    class Body:
        def __init__(self):
            self.text = "x" * 1_000_000

    def fail(*args, **kwargs):
        # At admission of the next batch, no prior failed response is active.
        gc.collect()
        retained.append(sum(ref() is not None for ref in references))
        body = Body()
        references.append(weakref.ref(body))
        raise ResponseIncomplete("evidence_output_invalid", stage)

    def failed_batches(batch, operation, **kwargs):
        yield from retry_batches(batch, fail, **kwargs)
        gc.collect()
        retained.append(sum(ref() is not None for ref in references))

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        value = evidence_response(payload)
        if payload["stage"] == "facts":
            for unit, row in zip(payload["units"], value["units"], strict=True):
                if unit["kind"] == "heading":
                    row.update(facts=[], empty_reason="Organization")
                else:
                    for fact in row["facts"]:
                        fact["topic"] = unit["headings"][-1]
        elif payload["stage"] == "planning":
            value = {
                "topics": [
                    {
                        "name": title.lower().replace(" ", "-"),
                        "title": title,
                        "kind": "concept",
                        "members": [uid],
                    }
                    for uid, title in payload["topic_labels"].items()
                ]
            }
        return response(value)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(evidence_plan, "MAX_PLAN_TOPICS", 1)
    if stage == "generation":
        monkeypatch.setattr(evidence_pages, "generate_topic", fail)
    else:
        module = evidence_facts if stage == "facts" else evidence_plan
        monkeypatch.setattr(module, "retry_batches", failed_batches)
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed"
    assert len(references) >= 2
    assert retained and max(retained) == 0, retained
    assert any(
        row["stage"] == stage and row["reason"] == "evidence_output_invalid"
        for row in result.omissions
    )


@pytest.mark.parametrize("changed_contract", [False, True])
def test_body_release_upgrade_reuses_only_identical_completed_facts(
    kb_dir, tmp_path, monkeypatch, changed_contract
):
    from openkb.agent import evidence_checkpoints, shared_analysis
    from openkb.agent.fact_resume import PRE_FAILURE_BODY_RELEASE
    from openkb.application.source_actions import continue_source

    original_revision = evidence_checkpoints.module_revision

    def preceding_revision(module):
        name = module.removeprefix("openkb.agent.")
        if changed_contract and name == "evidence_quotes":
            return "0" * 64
        return PRE_FAILURE_BODY_RELEASE.get(name, original_revision(module))

    stages = []

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stages.append(payload["stage"])
        return response(evidence_response(payload))

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(evidence_checkpoints, "module_revision", preceding_revision)
    monkeypatch.setattr(shared_analysis, "module_revision", preceding_revision)
    source = tmp_path / "saved-facts.md"
    source.write_text("# Recovery\n\nKeep the original backup intact.")
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed"
    stages.clear()
    monkeypatch.setattr(evidence_checkpoints, "module_revision", original_revision)
    monkeypatch.setattr(shared_analysis, "module_revision", original_revision)
    resumed = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert resumed.knowledge_compilation == "completed"
    assert ("facts" in stages) == changed_contract
