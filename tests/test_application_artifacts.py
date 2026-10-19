"""Real artifact listing, graph generation, and unique complete exports."""

import zipfile

import pytest
from artifact_fixtures import publish_artifacts


def test_artifact_export_allows_a_profile_containing_global_lifecycle_state(kb_dir, tmp_path):
    from ultrakb.application.artifacts import export_artifact

    profile = tmp_path.parent / f"{tmp_path.name}-profile"
    (profile / ".ultrakb/kb-lifecycle").mkdir(parents=True)
    destination = profile / "Documents/exports"
    destination.mkdir(parents=True)
    source = kb_dir / "output/result.md"
    source.parent.mkdir(exist_ok=True)
    source.write_text("Complete artifact", encoding="utf-8")
    publish_artifacts(kb_dir, source.parent)

    exported = export_artifact(kb_dir, "output/result.md", destination)
    assert exported.read_text(encoding="utf-8") == "Complete artifact"


def test_artifact_export_rejects_a_native_knowledge_base_without_a_wiki_copy(kb_dir, tmp_path):
    from ultrakb.application.artifacts import export_artifact
    from ultrakb.application.knowledge_bases import initialize_kb

    target = tmp_path.parent / f"{tmp_path.name}-unmaterialized"
    source = kb_dir / "output/result.md"
    source.parent.mkdir(exist_ok=True)
    source.write_text("Complete artifact", encoding="utf-8")
    publish_artifacts(kb_dir, source.parent)
    initialize_kb(target, seed_environment=False)
    (target / "wiki").rename(target / "saved-working-copy")
    with pytest.raises(ValueError, match="outside every knowledge base"):
        export_artifact(kb_dir, "output/result.md", target)
    assert not (target / "result.md").exists()


def test_artifact_export_keeps_supporting_files_and_existing_exports(kb_dir, tmp_path):
    from ultrakb.application.artifacts import export_artifact, list_artifacts

    target = kb_dir / "output/skills/demo"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# Demo")
    (target / "references").mkdir()
    (target / "references/topic.md").write_text("Supporting knowledge")
    publish_artifacts(kb_dir, target)
    export_dir = tmp_path.parent / f"{tmp_path.name}-export"
    export_dir.mkdir()
    artifacts = list_artifacts(kb_dir)
    assert any(item.path == "output/skills/demo" for item in artifacts)
    first = export_artifact(kb_dir, "output/skills/demo", export_dir)
    second = export_artifact(kb_dir, "output/skills/demo", export_dir)
    assert first != second
    with zipfile.ZipFile(first) as archive:
        assert archive.read("demo/references/topic.md") == b"Supporting knowledge"
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(ValueError):
        export_artifact(kb_dir, ".ultrakb/config.yaml", export_dir)
    other = export_dir / "other-kb"
    (other / ".ultrakb").mkdir(parents=True)
    (other / ".ultrakb/needs-repair.json").write_text("{}")
    destination = other / "wiki/concepts"
    destination.mkdir(parents=True)
    with pytest.raises(ValueError):
        export_artifact(kb_dir, "output/skills/demo", destination)
    assert not list(destination.iterdir())


def test_graph_generation_preserves_links_and_replaces_only_its_fixed_artifact(kb_dir):
    from ultrakb.application.artifacts import generate_graph, read_graph

    (kb_dir / "wiki/concepts/a.md").write_text("# A\n[[concepts/b]]")
    (kb_dir / "wiki/concepts/b.md").write_text("# B")
    from publication_read_inputs import publish_fixture

    publish_fixture(kb_dir)
    result = generate_graph(kb_dir)
    assert result.path == kb_dir / "output/visualize/graph.html"
    assert result.graph["edges"] == [{"source": "concepts/a", "target": "concepts/b"}]
    assert read_graph(kb_dir) == result.graph
    result.path.write_text("Old graph")
    again = generate_graph(kb_dir)
    assert again.path is None and again.reason_code == "artifact_sync_changed"
    from ultrakb.application.artifacts import read_artifact

    assert "<canvas" in read_artifact(kb_dir, "output/visualize/graph.html")
    assert result.path.read_text() == "Old graph"
    assert not list((kb_dir / ".ultrakb/journal").glob("*.json"))
