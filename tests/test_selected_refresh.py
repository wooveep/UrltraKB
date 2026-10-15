"""Refresh affected contributions without recompiling normally imported documents."""

import asyncio

from application_llm_fixtures import real_application_model  # noqa: F401
from test_query_views import _import_rule


def test_selected_refresh_reuses_current_summaries_without_model_work(kb_dir, monkeypatch):
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status
    from ultrakb.application.views import view_scope

    problem = _import_rule(kb_dir, monkeypatch, "problem", "Old operating rule.")
    normal = _import_rule(kb_dir, monkeypatch, "normal", "Independent operating rule.")
    normal_summary = kb_dir / "wiki/summaries/normal.md"
    before = normal_summary.read_bytes()
    fixed = _import_rule(kb_dir, monkeypatch, "problem", "Corrected operating rule.")
    assert fixed.source_id == problem.source_id
    scope = view_scope(kb_dir, normal.units[0].view_id)
    assert "summaries/normal.md" in refresh_status(kb_dir, scope=scope)["needs_refresh"]
    retained = {
        str(p.relative_to(kb_dir)): p.read_bytes()
        for p in (kb_dir / ".ultrakb/normalized").rglob("*")
        if p.is_file()
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("Current summaries must not be recompiled or sent to a model")

    monkeypatch.setattr("ultrakb.agent.compiler.compile_short_doc", forbidden)
    monkeypatch.setattr("ultrakb.agent.compiler.compile_long_doc", forbidden)
    monkeypatch.setattr("litellm.completion", forbidden)
    monkeypatch.setattr("litellm.acompletion", forbidden)

    result = asyncio.run(refresh_selected_sources(kb_dir, [problem.source_id], scope=scope))

    assert result.status == "completed", result.message
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"] == {}
    assert normal_summary.read_bytes() == before
    assert all((kb_dir / name).read_bytes() == content for name, content in retained.items())


def test_selected_refresh_cannot_certify_a_manually_changed_normal_summary(kb_dir, monkeypatch):
    from ultrakb.application.pages import read_page, save_page
    from ultrakb.application.refresh import refresh_selected_sources, refresh_status
    from ultrakb.application.views import view_scope

    problem = _import_rule(kb_dir, monkeypatch, "problem", "Old operating rule.")
    normal = _import_rule(kb_dir, monkeypatch, "normal", "Independent operating rule.")
    _import_rule(kb_dir, monkeypatch, "problem", "Corrected operating rule.")
    scope = view_scope(kb_dir, normal.units[0].view_id)
    page = read_page(kb_dir, "summaries/normal", scope=scope)
    save_page(
        kb_dir, page.path, "Manual statement needing review.", version=page.version, scope=scope
    )
    before = (scope.wiki_dir / "summaries/normal.md").read_bytes()
    reason = refresh_status(kb_dir, scope=scope)["needs_refresh"]["summaries/normal.md"]

    result = asyncio.run(refresh_selected_sources(kb_dir, [problem.source_id], scope=scope))

    assert result.status == "partial", result.message
    assert refresh_status(kb_dir, scope=scope)["needs_refresh"]["summaries/normal.md"] == reason
    assert (scope.wiki_dir / "summaries/normal.md").read_bytes() == before
