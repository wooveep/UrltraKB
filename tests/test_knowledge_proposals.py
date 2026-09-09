"""Whole-document publication protects all bytes and binds manual acceptance."""

from functools import partial

from openkb.evidence import BlockDraft, ParseStore
from openkb.inputs import prepared_input
from openkb.knowledge_commit import KnowledgeWorkspace
from openkb.knowledge_commit import accept_proposal as accept
from openkb.knowledge_commit import publish_proposal as publish
from openkb.locks import atomic_write_text
from openkb.sources import SourceStore, content_id

accept_proposal = partial(accept, config_id=content_id({"model": "test"}))
publish_proposal = partial(publish, config_id=content_id({"model": "test"}))


def inputs(kb, tmp):
    file = tmp / "manual.txt"
    file.write_text("Source facts")
    with prepared_input(file) as ready:
        source = SourceStore(kb).intake(ready)
    parsed = ParseStore(kb).save(
        source,
        {"parser": "test-v1"},
        [BlockDraft("Source facts", "paragraph", {"kind": "text", "line": 1})],
    )
    ParseStore(kb).select(source, parsed)
    return source, parsed


def test_private_generation_has_no_live_writes_and_publishes_whole_source(kb_dir, tmp_path):
    source, parsed = inputs(kb_dir, tmp_path)
    with KnowledgeWorkspace(kb_dir, source, parsed, {"model": "test"}) as workspace:
        atomic_write_text(workspace.path / "wiki/entities/one.md", "Generated one")
        atomic_write_text(workspace.path / "wiki/concepts/two.md", "Generated two")
        assert not (kb_dir / "wiki/entities/one.md").exists()
        proposal = workspace.proposal()
    assert publish_proposal(kb_dir, proposal.id).status == "completed"
    assert (kb_dir / "wiki/entities/one.md").read_text() == "Generated one"
    assert (kb_dir / "wiki/concepts/two.md").read_text() == "Generated two"


def test_unknown_or_modified_full_page_requires_exact_acceptance(kb_dir, tmp_path):
    source, parsed = inputs(kb_dir, tmp_path)
    page = kb_dir / "wiki/entities/one.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntitle: Human metadata\n---\nHuman content")
    with KnowledgeWorkspace(kb_dir, source, parsed, {"model": "test"}) as workspace:
        atomic_write_text(workspace.path / "wiki/entities/one.md", "Proposed")
        proposal = workspace.proposal()
    assert publish_proposal(kb_dir, proposal.id).status == "needs_acceptance"
    assert page.read_text().endswith("Human content")
    accept_proposal(kb_dir, proposal.id, ["entities/one.md"])
    page.write_text("Another edit after acceptance")
    assert publish_proposal(kb_dir, proposal.id).status == "input_conflict"
    assert page.read_text() == "Another edit after acceptance"


def test_known_generated_baseline_allows_update_but_detects_frontmatter_edits(kb_dir, tmp_path):
    source, parsed = inputs(kb_dir, tmp_path)
    page = kb_dir / "wiki/entities/one.md"
    for text in ("---\ntitle: Original\n---\nBody", "---\ntitle: Updated\n---\nBody"):
        with KnowledgeWorkspace(kb_dir, source, parsed, {"model": "test"}) as workspace:
            atomic_write_text(workspace.path / "wiki/entities/one.md", text)
            proposal = workspace.proposal()
        assert publish_proposal(kb_dir, proposal.id).status == "completed"
    page.write_text(page.read_text().replace("Updated", "Manual edit"))
    with KnowledgeWorkspace(kb_dir, source, parsed, {"model": "test"}) as workspace:
        atomic_write_text(workspace.path / "wiki/entities/one.md", "Replacement")
        proposal = workspace.proposal()
    assert publish_proposal(kb_dir, proposal.id).status == "needs_acceptance"


def test_changes_to_other_wiki_inputs_invalidate_proposal(kb_dir, tmp_path):
    source, parsed = inputs(kb_dir, tmp_path)
    with KnowledgeWorkspace(kb_dir, source, parsed, {"model": "test"}) as workspace:
        atomic_write_text(workspace.path / "wiki/entities/one.md", "Proposed")
        proposal = workspace.proposal()
    (kb_dir / "wiki/entities").mkdir(exist_ok=True)
    (kb_dir / "wiki/entities/new-context.md").write_text("New context after generation")
    assert publish_proposal(kb_dir, proposal.id).status == "input_conflict"
    assert not (kb_dir / "wiki/entities/one.md").exists()


def test_reparse_invalidates_accepted_generation_but_old_evidence_survives(kb_dir, tmp_path):
    from openkb.evidence import Evidence

    source, parsed = inputs(kb_dir, tmp_path)
    page = kb_dir / "wiki/entities/manual.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("Human edit")
    with KnowledgeWorkspace(kb_dir, source, parsed, {"model": "test"}) as workspace:
        atomic_write_text(workspace.path / "wiki/entities/manual.md", "Proposed")
        proposal = workspace.proposal()
    accept_proposal(kb_dir, proposal.id, ["entities/manual.md"])
    ParseStore(kb_dir).save(
        source,
        parsed.profile,
        [BlockDraft("Corrected source facts", "paragraph", {"kind": "text", "line": 1})],
    )
    assert publish_proposal(kb_dir, proposal.id).status == "input_conflict"
    reference = Evidence(source.source_id, source.id, parsed.id, parsed.blocks[0].id)
    assert ParseStore(kb_dir).read(reference, max_chars=100).text == "Source facts"
    assert page.read_text() == "Human edit"


def test_external_edit_during_generation_preserves_original_bytes_and_rejects_publication(
    kb_dir, tmp_path
):
    source, parsed = inputs(kb_dir, tmp_path)
    page = kb_dir / "wiki/entities/manual.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("Before generation")
    with KnowledgeWorkspace(kb_dir, source, parsed, {"model": "test"}) as workspace:
        atomic_write_text(workspace.path / "wiki/entities/manual.md", "Proposed replacement")
        page.write_text("External edit while generating")
        proposal = workspace.proposal()
    assert publish_proposal(kb_dir, proposal.id).status == "input_conflict"
    assert page.read_text() == "External edit while generating"
    store = SourceStore(kb_dir)
    digest = store.put_bytes(b"Before generation")
    assert digest == proposal.before["entities/manual.md"]
    assert store.asset(digest).read_bytes() == b"Before generation"
