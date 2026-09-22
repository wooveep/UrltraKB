"""Publish verified siblings while keeping excluded content explicit and recoverable."""

import json
from collections import Counter

import litellm
import pytest
import yaml

from openkb.application.documents import DocumentResult, import_document
from openkb.application.source_actions import continue_source
from openkb.config import DEFAULT_CONFIG
from openkb.processing import ProcessingIncomplete
from tests.processing_fixtures import OFFLINE_PROCESSING
from tests.test_adaptive_processing import response


@pytest.fixture
def setup(kb_dir, tmp_path, monkeypatch):
    config = {
        **DEFAULT_CONFIG,
        "model": "openai/offline-test",
        "language": "en",
        "navigation": {"enabled": False},
        "processing": {**OFFLINE_PROCESSING, "concurrency": 2},
    }
    (kb_dir / ".openkb/config.yaml").write_text(yaml.safe_dump(config))
    source = tmp_path / "manual.md"
    source.write_text("Alpha requirement.\n\nBeta requirement.")
    calls = Counter()
    state = {"stage": "verification", "broken": True, "global": None}

    def plan(payload):
        changes = []
        existing = set(payload.get("existing_targets", []))
        registered = {
            item["name"]: item
            for item in payload["carry"]["page_register"]
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        for block in payload["evidence"]["blocks"]:
            text = block["text"]
            title = "Alpha" if "Alpha" in text else "Beta"
            name = f"concepts/{title.lower()}"
            prior = registered.get(name)
            changes.append(
                {
                    "local_key": title.lower(),
                    "target_key": prior["key"] if prior else "",
                    "target": prior.get("target", "")
                    if prior
                    else name
                    if name in existing
                    else "",
                    "kind": "concept",
                    "name": name,
                    "title": title,
                    "purpose": f"{title} requirement",
                    "subject_ranges": [[block["order"], block["order"] + 1]],
                    "necessary_context": [],
                }
            )
        target = payload["target"]
        ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
        return {
            "overview": {"text": "Requirements overview.", "ranges": ranges, "limitations": []},
            "page_changes": changes,
            "source_only": [],
            "unresolved": [],
            "resolutions": [],
        }

    def completion(**kwargs):
        payload = json.loads(kwargs["messages"][-1]["content"])
        stage = payload["stage"]
        calls[stage] += 1
        if state["global"] and stage == "generation":
            raise state["global"]
        if stage == "planning":
            return response(plan(payload))
        if stage == "generation":
            title = payload["page"]["title"]
            if state.get("unavailable_topic") == title:
                raise litellm.ServiceUnavailableError(
                    "Busy", model="offline-test", llm_provider="openai"
                )
            if state["broken"] and state["stage"] == stage and title == "Beta":
                return response({"content": "# Beta\nBeta requirement.", "covered": []})
            content = f"# {title}\n{title} requirement."
            if title == "Alpha":
                content += "\nSee [[concepts/beta|Beta]].\n`[[concepts/beta]]`"
            return response(
                {
                    "content": content,
                    "covered": [item["id"] for item in payload["occurrences"]],
                }
            )
        if stage == "verification" and state["broken"] and state["stage"] == stage:
            if payload["page"]["title"] == "Beta":
                return response(
                    {
                        "verdict": "unsupported",
                        "reason": "Unsupported claim.",
                        "issues": ["Beta is intentionally unavailable in this test."],
                    }
                )
        return response({"verdict": "supported", "reason": "Supported by the source."})

    monkeypatch.setattr(litellm, "completion", completion)
    return source, state, calls


@pytest.mark.parametrize("stage", ["generation", "verification"])
def test_local_failure_publishes_only_verified_content(kb_dir, setup, stage):
    source, state, calls = setup
    state["stage"] = stage
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", (result.reason, result)
    assert result.omissions and result.omissions[0]["stage"] == "generation"
    assert "knowledge_content_omitted" in result.warnings
    assert result.coverage["status"] == "partial"
    assert result.coverage["ranges"]
    assert any(row["status"] == "pending" for row in result.coverage["ranges"])
    assert (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    body = (kb_dir / "wiki/concepts/alpha.md").read_text()
    assert "See Beta." in body
    assert "`[[concepts/beta]]`" in body
    summary = next((kb_dir / "wiki/summaries").glob("*.md")).read_text()
    assert "内容遗漏" in summary and "[[concepts/beta" not in summary
    assert DocumentResult.from_summary(json.loads(json.dumps(result.__dict__))) == result
    before = calls.copy()
    repeated = import_document(kb_dir, source)
    assert repeated.status == "skipped" and repeated.omissions == result.omissions
    assert repeated.coverage == result.coverage
    assert calls == before


def test_explicit_continue_can_complete_excluded_work(kb_dir, setup):
    source, state, calls = setup
    state["stage"] = "generation"
    first = import_document(kb_dir, source)
    assert first.omissions
    state["broken"] = False
    second = continue_source(kb_dir, first.source_id, version_id=first.input_version)
    assert second.knowledge_compilation == "completed", second
    assert not second.omissions
    assert second.coverage["status"] == "complete"
    assert (kb_dir / "wiki/concepts/beta.md").exists()
    assert "内容遗漏" not in next((kb_dir / "wiki/summaries").glob("*.md")).read_text()


def test_exhausted_temporary_failure_skips_only_affected_topic(kb_dir, setup):
    source, state, calls = setup
    state.update(broken=False, unavailable_topic="Beta")
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation == "completed", result
    assert result.coverage["status"] == "partial"
    assert (kb_dir / "wiki/concepts/alpha.md").exists()
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    assert any(row["reason"] == "provider_temporarily_unavailable" for row in result.omissions)


@pytest.mark.parametrize(
    "error",
    [
        ProcessingIncomplete("request_budget_exhausted", "generation"),
        RuntimeError("service unavailable"),
    ],
)
def test_global_failure_does_not_become_content_omission(kb_dir, setup, error):
    source, state, calls = setup
    state["global"] = error
    result = import_document(kb_dir, source)
    assert result.knowledge_compilation != "completed"
    assert not list((kb_dir / "wiki/concepts").glob("*.md"))


def test_new_version_withdraws_old_contribution_for_excluded_topic(kb_dir, setup):
    source, state, calls = setup
    state["broken"] = False
    first = import_document(kb_dir, source)
    assert first.knowledge_compilation == "completed"
    source.write_text("Alpha requirement updated.\n\nBeta requirement updated.")
    state["broken"] = True
    second = import_document(kb_dir, source)
    assert second.knowledge_compilation == "completed", second
    assert second.omissions
    assert not (kb_dir / "wiki/concepts/beta.md").exists()
    assert "See Beta." in (kb_dir / "wiki/concepts/alpha.md").read_text()


@pytest.mark.parametrize("position", ["outside", "inside"])
@pytest.mark.parametrize("operation", ["remove", "update"])
@pytest.mark.parametrize("identity", ["intact", "no_metadata", "unmarked"])
def test_accepted_cross_source_link_cleanup_keeps_manual_summary(
    kb_dir, tmp_path, setup, position, operation, identity
):
    from openkb.application.removal import remove_document
    from openkb.application.source_actions import review_source_proposal

    source, state, _ = setup
    state["broken"] = False
    first_source = tmp_path / "first.md"
    first_source.write_text("Alpha requirement.")
    first = import_document(kb_dir, first_source)
    assert first.knowledge_compilation == "completed", first
    second = import_document(kb_dir, source)
    assert second.knowledge_compilation == "completed", second
    summary = next(
        p for p in (kb_dir / "wiki/summaries").glob("*.md") if first.source_id in p.read_text()
    )
    note = "\nHuman note: retain this interpretation of [[concepts/beta|Beta]].\n"
    text = summary.read_text()
    closing = f"<!-- /openkb-source:{first.source_id} -->"
    summary.write_text(
        text + note if position == "outside" else text.replace(closing, note + closing)
    )
    if identity != "intact":
        from openkb import frontmatter

        text = frontmatter.drop_line(summary.read_text(), "source_id")
        if identity == "unmarked":
            text = text.replace(f"<!-- openkb-source:{first.source_id} -->", "").replace(
                closing, ""
            )
        summary.write_text(text)
    source.write_text("Alpha requirement updated.")
    updated = import_document(kb_dir, source)
    assert updated.reason == "needs_acceptance", updated
    review = review_source_proposal(kb_dir, updated.resume)
    accepted = continue_source(
        kb_dir,
        updated.source_id,
        version_id=updated.input_version,
        proposal_id=updated.resume,
        accept_pages=review["protected"],
    )
    assert accepted.knowledge_compilation == "completed", accepted
    assert "Human note: retain this interpretation of Beta." in summary.read_text()
    before = summary.read_bytes()
    if operation == "remove":
        removed = remove_document(kb_dir, first.source_id)
        assert removed.status == "removed", removed
        assert summary.read_bytes() == before
        assert summary.relative_to(kb_dir).as_posix() in removed.retained
    else:
        first_source.write_text("Alpha requirement revised.")
        updated = import_document(kb_dir, first_source)
        assert updated.reason == "needs_acceptance", updated
        assert summary.read_bytes() == before
        review = review_source_proposal(kb_dir, updated.resume)
        assert summary.relative_to(kb_dir / "wiki").as_posix() in review["protected"]
        accepted = continue_source(
            kb_dir,
            updated.source_id,
            version_id=updated.input_version,
            proposal_id=updated.resume,
            accept_pages=review["protected"],
        )
        assert accepted.knowledge_compilation == "completed", accepted
        assert "Human note" not in summary.read_text()
