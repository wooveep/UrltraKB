"""Coordinated names survive publication and a fresh full-registry capture."""

import hashlib

import pytest

from ultrakb import frontmatter
from ultrakb.agent.compile_delivery import publish_compile_results
from ultrakb.agent.compile_plan import CompilePlan, PlanGroup
from ultrakb.agent.link_registry import FrozenLinkRegistry
from ultrakb.agent.page_updates import UnchangedPage
from ultrakb.locks import atomic_write_text

pytestmark = pytest.mark.integration


def publish(wiki, kind, *, aliases, update=False, unchanged=False):
    item = {"name": "canonical", "title": "Canonical", "aliases": aliases, "type": "product"}
    group = PlanGroup(**{"update" if update else "create": [item]})
    plan = (
        CompilePlan(group, PlanGroup()) if kind == "concepts" else CompilePlan(PlanGroup(), group)
    )
    if unchanged:
        text = (wiki / kind / "canonical.md").read_bytes()
        content = UnchangedPage(hashlib.sha256(text).hexdigest(), "Already covered.")
    else:
        content = "Updated body.\n" if update else "Initial body.\n"
    results = (
        {"concept_results": [("canonical", content, update, "Description.")]}
        if kind == "concepts"
        else {"entity_results": [("canonical", content, "Description.", "product")]}
    )
    publish_compile_results(
        wiki, "manual", plan, summary="Source summary.", rewrite_summary=True, **results
    )


@pytest.mark.parametrize("kind", ["concepts", "entities"])
def test_plan_aliases_merge_on_create_and_update_without_guessing_paths(kb_dir, kind):
    wiki = kb_dir / "wiki"
    atomic_write_text(wiki / "sources/manual.md", "Source text.\n")
    publish(wiki, kind, aliases=["Friendly name", "Second name"])
    path = wiki / kind / "canonical.md"
    assert frontmatter.parse(path.read_text())["aliases"] == ["Friendly name", "Second name"]
    body = frontmatter.split(path.read_text())[1]
    atomic_write_text(
        path,
        "---\ntype: Product\naliases:\n  - Friendly name\n"
        "  - Second name\npinned: true\nsources: [summaries/manual.md]\n---\n" + body,
    )
    publish(wiki, kind, aliases=["Second name", "New name"], update=True)
    text = path.read_text()
    metadata = frontmatter.parse(text)
    assert metadata["aliases"] == ["Friendly name", "Second name", "New name"]
    assert metadata["pinned"] is True and "pinned: true\n" in text
    assert "Updated body." in text
    registry = FrozenLinkRegistry.capture(wiki, {f"{kind}/canonical"})
    for alias in metadata["aliases"]:
        assert f"{kind}/canonical" in registry.render(f"[[{alias}]]")
    assert [page.name for page in (wiki / kind).glob("*.md")] == ["canonical.md"]


@pytest.mark.parametrize("kind", ["concepts", "entities"])
def test_unchanged_body_keeps_source_basis_while_remembering_coordinated_alias(kb_dir, kind):
    wiki = kb_dir / "wiki"
    publish(wiki, kind, aliases=["Existing name"])
    path = wiki / kind / "canonical.md"
    before = path.read_text()
    publish(wiki, kind, aliases=["New name"], update=True, unchanged=True)
    after = path.read_text()
    assert frontmatter.split(after)[1] == frontmatter.split(before)[1]
    assert frontmatter.parse(after)["sources"] == frontmatter.parse(before)["sources"]
    assert frontmatter.parse(after)["aliases"] == ["Existing name", "New name"]


@pytest.mark.parametrize("kind", ["concepts", "entities"])
def test_invalid_plan_aliases_are_rejected_before_any_wiki_write(kb_dir, kind):
    wiki = kb_dir / "wiki"
    publish(wiki, kind, aliases=["Existing name"])
    before = {str(path): path.read_bytes() for path in wiki.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="aliases"):
        publish(wiki, kind, aliases=["Valid", 19], update=True)
    assert {str(path): path.read_bytes() for path in wiki.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("kind", ["concepts", "entities"])
def test_alias_only_publication_preserves_original_body_line_endings(kb_dir, kind):
    wiki = kb_dir / "wiki"
    publish(wiki, kind, aliases=[])
    path = wiki / kind / "canonical.md"
    text = (
        "---\r\ntype: Product\r\npinned: true\r\nsources: [summaries/manual.md]\r\n---\r\n"
        "\r\nOriginal body.\r\nSecond line.\r\n"
    )
    atomic_write_text(path, text)
    publish(wiki, kind, aliases=["New name"], update=True, unchanged=True)
    updated = path.read_bytes().decode("utf-8")
    assert frontmatter.split(updated)[1] == frontmatter.split(text)[1]
    assert frontmatter.parse(updated)["aliases"] == ["New name"]
    assert "pinned: true\r\n" in updated


@pytest.mark.parametrize("kind", ["concepts", "entities"])
@pytest.mark.parametrize("reference", ["aliases: *names\n", "aliases:\n  *names\n"])
def test_alias_reference_merges_only_its_own_field(kb_dir, kind, reference):
    wiki = kb_dir / "wiki"
    publish(wiki, kind, aliases=[])
    path = wiki / kind / "canonical.md"
    definition = "names: &names [Friendly name, Second name]\n"
    text = (
        "---\ntype: Product\n"
        + definition
        + reference
        + "shared_names: *names\npinned: true\nsources: [summaries/manual.md]\n---\n"
        + "\nOriginal body.\n"
    )
    atomic_write_text(path, text)
    publish(wiki, kind, aliases=["New name"], update=True, unchanged=True)
    updated = path.read_bytes().decode("utf-8")
    metadata = frontmatter.parse(updated)
    assert metadata["aliases"] == ["Friendly name", "Second name", "New name"]
    assert metadata["names"] == metadata["shared_names"] == ["Friendly name", "Second name"]
    assert definition in updated and "shared_names: *names\npinned: true\n" in updated
    assert frontmatter.split(updated)[1] == frontmatter.split(text)[1]
    registry = FrozenLinkRegistry.capture(wiki, {f"{kind}/canonical"})
    assert f"{kind}/canonical" in registry.render("[[New name]]")


@pytest.mark.parametrize("kind", ["concepts", "entities"])
@pytest.mark.parametrize("unsupported", ["shared_anchor", "unknown_tag"])
def test_unsupported_alias_metadata_rejects_the_whole_batch_before_writes(
    kb_dir, kind, unsupported
):
    wiki = kb_dir / "wiki"
    publish(wiki, kind, aliases=[])
    prefix = (
        "aliases: &names [Existing name]\nshared_names: *names\n"
        if unsupported == "shared_anchor"
        else "aliases: [Existing name]\ncustom: !unknown value\n"
    )
    atomic_write_text(
        wiki / kind / "canonical.md",
        "---\ntype: Product\n" + prefix + "sources: [summaries/manual.md]\n---\nOriginal.\n",
    )
    atomic_write_text(
        wiki / kind / "a-safe.md",
        "---\ntype: Product\naliases: [Safe name]\nsources: [summaries/manual.md]\n---\nSafe.\n",
    )
    updates = [
        {"name": "a-safe", "title": "Safe", "aliases": ["Safe new name"], "type": "product"},
        {"name": "canonical", "title": "Canonical", "aliases": ["New name"], "type": "product"},
    ]
    group = PlanGroup(update=updates)
    plan = (
        CompilePlan(group, PlanGroup()) if kind == "concepts" else CompilePlan(PlanGroup(), group)
    )
    results = (
        {
            "concept_results": [
                (item["name"], "Updated body.\n", True, "Description.") for item in updates
            ]
        }
        if kind == "concepts"
        else {
            "entity_results": [
                (item["name"], "Updated body.\n", "Description.", "product") for item in updates
            ]
        }
    )
    before = {str(path): path.read_bytes() for path in wiki.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="anchor|frontmatter"):
        publish_compile_results(
            wiki, "manual", plan, summary="Changed summary.", rewrite_summary=True, **results
        )
    assert {str(path): path.read_bytes() for path in wiki.rglob("*") if path.is_file()} == before
