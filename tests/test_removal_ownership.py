"""Deleting source knowledge preserves manual authorship through the public operation."""

import pytest

from openkb.application.documents import import_document
from openkb.application.removal import preview_removal, remove_document


@pytest.mark.parametrize(
    "edit",
    ["none", "outside", "inside", "unknown_baseline", "accepted_outside", "accepted_metadata"],
)
def test_last_source_removal_obeys_known_generated_ownership(kb_dir, tmp_path, model_service, edit):
    source = tmp_path / "procedure.md"
    source.write_text("The timeout is 42 seconds.")
    if edit == "accepted_outside":
        (kb_dir / "wiki/concepts/notes.md").write_text(
            "# Notes\n\nHuman instruction: retain this independent note.\n"
        )
    if edit == "accepted_metadata":
        (kb_dir / "wiki/concepts/notes.md").write_text(
            '---\nhuman_instruction: "Retain independent metadata"\n---\n'
        )
    imported = import_document(kb_dir, source)
    if edit in {"accepted_outside", "accepted_metadata"}:
        from openkb.application.source_actions import continue_source, review_source_proposal

        assert imported.reason == "needs_acceptance", imported
        review = review_source_proposal(kb_dir, imported.resume)
        imported = continue_source(
            kb_dir,
            imported.source_id,
            version_id=imported.input_version,
            proposal_id=imported.resume,
            accept_pages=review["protected"],
        )
    assert imported.knowledge_compilation == "completed", imported
    page = next((kb_dir / "wiki/concepts").glob("*.md"))
    original = page.read_text()
    if edit == "outside":
        page.write_text(original + "\nHuman instruction: retain this independent note.\n")
    elif edit == "inside":
        marker = f"<!-- /openkb-source:{imported.source_id} -->"
        page.write_text(original.replace(marker, "Human correction: confirm first.\n" + marker))
    elif edit == "unknown_baseline":
        from openkb.knowledge_commit import _directory

        _directory(kb_dir, "baselines.json").unlink()
    before = {p: p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()}
    registry = (kb_dir / ".openkb/hashes.json").read_bytes()
    preview = preview_removal(kb_dir, imported.source_id)
    assert preview.plan is not None
    assert (page.stem in preview.plan.concept_deletes) == (edit == "none")
    if edit in {"inside", "unknown_baseline"}:
        with pytest.raises(ValueError, match="ownership|edited|baseline"):
            remove_document(kb_dir, imported.source_id, version=preview.version)
        assert before == {p: p.read_bytes() for p in (kb_dir / "wiki").rglob("*") if p.is_file()}
        assert (kb_dir / ".openkb/hashes.json").read_bytes() == registry
    else:
        outcome = remove_document(kb_dir, imported.source_id, version=preview.version)
        assert outcome.status == "removed", outcome
        assert not list((kb_dir / "wiki/summaries").glob("*.md"))
        assert page.exists() == (edit in {"outside", "accepted_outside", "accepted_metadata"})
        if page.exists():
            if edit == "accepted_metadata":
                assert 'human_instruction: "Retain independent metadata"' in page.read_text()
            else:
                assert "Human instruction: retain this independent note." in page.read_text()
            assert f"openkb-source:{imported.source_id}" not in page.read_text()
            assert "42 seconds" not in page.read_text()


@pytest.mark.parametrize("summary_state", ["edited", "unknown"])
def test_source_removal_keeps_manual_or_unclassified_summary(
    kb_dir, tmp_path, model_service, summary_state
):
    source = tmp_path / "summary.md"
    source.write_text("The timeout is 42 seconds.")
    imported = import_document(kb_dir, source)
    assert imported.knowledge_compilation == "completed", imported
    summary = next((kb_dir / "wiki/summaries").glob("*.md"))
    if summary_state == "edited":
        summary.write_text(summary.read_text() + "\nHuman note: retain my interpretation.\n")
    else:
        import json

        from openkb.knowledge_commit import _directory

        path = _directory(kb_dir, "baselines.json")
        records = json.loads(path.read_text())
        records.pop(summary.relative_to(kb_dir / "wiki").as_posix())
        path.write_text(json.dumps(records))
    before = summary.read_bytes()
    preview = preview_removal(kb_dir, imported.source_id)
    outcome = remove_document(kb_dir, imported.source_id, version=preview.version)
    assert outcome.status == "removed", outcome
    assert summary.read_bytes() == before
    assert summary.relative_to(kb_dir).as_posix() in outcome.retained
    assert any(
        action.tag == "KEEP" and "summaries/" in action.target for action in outcome.result.actions
    )


def test_new_version_preserves_accepted_metadata_on_retired_topic(kb_dir, tmp_path, model_service):
    import json

    from openkb.application.source_actions import continue_source, review_source_proposal
    from tests.http_model_fixture import evidence_response

    page = kb_dir / "wiki/concepts/notes.md"
    page.write_text('---\nhuman_instruction: "Retain independent metadata"\n---\n')
    source = tmp_path / "procedure.md"
    source.write_text("The timeout is 42 seconds.")
    first = import_document(kb_dir, source)
    assert first.reason == "needs_acceptance", first
    review = review_source_proposal(kb_dir, first.resume)
    first = continue_source(
        kb_dir,
        first.source_id,
        version_id=first.input_version,
        proposal_id=first.resume,
        accept_pages=review["protected"],
    )
    assert first.knowledge_compilation == "completed", first

    def respond(body):
        payload = json.loads(body["messages"][-1]["content"])
        response = evidence_response(payload)
        if payload["stage"] == "planning":
            target = payload["target"]
            ranges = target.get("ranges", [[target["target_start"], target["target_end"]]])
            response = {
                "overview": {
                    "text": "The current policy.",
                    "ranges": ranges,
                    "limitations": [],
                },
                "page_changes": [
                    {
                        "local_key": "new-topic",
                        "target_key": "",
                        "target": "",
                        "kind": "concept",
                        "name": "concepts/new-topic",
                        "title": "New topic",
                        "purpose": "The current policy.",
                        "subject_ranges": ranges,
                        "necessary_context": [],
                    }
                ],
                "source_only": [],
                "unresolved": [],
                "resolutions": [],
            }
        return response

    model_service.respond = respond
    source.write_text("The current policy is a separate topic.")
    changed = import_document(kb_dir, source)
    assert changed.knowledge_compilation == "completed", changed
    assert page.exists()
    assert 'human_instruction: "Retain independent metadata"' in page.read_text()
    assert first.source_id not in page.read_text()
    assert (kb_dir / "wiki/concepts/new-topic.md").exists()
