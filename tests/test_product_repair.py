import json
from types import SimpleNamespace

from test_query_views import _import_rule


def test_identity_batch_resumes_failed_publication_without_rewriting_prior_sources(
    kb_dir, monkeypatch
):
    from openkb.application.product_repair import (
        preview_identity_repair,
        resume_identity_repair,
        start_identity_repair,
    )
    from openkb.application.products import list_products
    from openkb.application.query_views import resolve_query_views

    first = _import_rule(
        kb_dir,
        monkeypatch,
        "practice",
        "9.4.0",
        "Practice.",
        product="CNware WinStack",
        family="practice",
    )
    other = _import_rule(
        kb_dir,
        monkeypatch,
        "ops",
        "9.4.0",
        "Operations.",
        product="CNware WinStack Platform",
        family="ops",
    )
    target = next(p for p in list_products(kb_dir) if p.name == "CNware WinStack")
    preview = preview_identity_repair(
        kb_dir, target.product_id, (first.source_id, other.source_id), aliases=("WinStack",)
    )
    assert not (kb_dir / ".openkb/catalog/identity-repairs").exists()
    assert preview["sources"][1]["metadata"]["applicable_versions"] == ["9.4.0"]
    repair = start_identity_repair(kb_dir, preview)
    failed = True
    calls = 0

    def model(**kwargs):
        nonlocal calls
        calls += 1
        if failed:
            raise ValueError("Fixture transient failure")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "description": "Operations",
                                "content": "Operations.",
                                "create": [],
                                "update": [],
                                "related": [],
                            }
                        )
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr("litellm.completion", model)
    result = resume_identity_repair(kb_dir, repair.repair_id)
    assert result.status == "partial"
    from openkb.application.reprocessing import preview_reprocessing, reprocess_source

    preview = preview_reprocessing(kb_dir, other.source_id)
    assert preview["status"] == "ready", preview["blockers"]
    failed = False
    reprocessed = reprocess_source(kb_dir, other.source_id, version=preview["version"])
    assert reprocessed.status == "added", reprocessed.message
    result = resume_identity_repair(kb_dir, repair.repair_id)
    assert result.status == "completed"
    previous_calls = calls
    assert resume_identity_repair(kb_dir, repair.repair_id) == result
    assert calls == previous_calls
    views = resolve_query_views(kb_dir, "WinStack V9.4.0").views
    assert len(views) == 1 and set(views[0].source_revision_ids) == {
        first.source_revision_id,
        reprocessed.source_revision_id,
    }
