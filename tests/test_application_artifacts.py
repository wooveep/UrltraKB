"""Real artifact listing, graph generation, and unique complete exports."""

import zipfile

import pytest


def test_artifact_export_keeps_supporting_files_and_existing_exports(kb_dir, tmp_path):
    from openkb.application.artifacts import export_artifact, list_artifacts

    target = kb_dir / "output/skills/demo"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# Demo")
    (target / "references").mkdir()
    (target / "references/topic.md").write_text("Supporting knowledge")
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
        export_artifact(kb_dir, ".openkb/config.yaml", export_dir)
    other = export_dir / "other-kb"
    (other / ".openkb").mkdir(parents=True)
    (other / ".openkb/needs-repair.json").write_text("{}")
    destination = other / "wiki/concepts"
    destination.mkdir(parents=True)
    with pytest.raises(ValueError):
        export_artifact(kb_dir, "output/skills/demo", destination)
    assert not list(destination.iterdir())


def test_graph_generation_preserves_links_and_replaces_only_its_fixed_artifact(kb_dir):
    from openkb.application.artifacts import generate_graph, read_graph

    (kb_dir / "wiki/concepts/a.md").write_text("# A\n[[concepts/b]]")
    (kb_dir / "wiki/concepts/b.md").write_text("# B")
    result = generate_graph(kb_dir)
    assert result.path == kb_dir / "output/visualize/graph.html"
    assert result.graph["edges"] == [{"source": "concepts/a", "target": "concepts/b"}]
    assert read_graph(kb_dir) == result.graph
    result.path.write_text("Old graph")
    again = generate_graph(kb_dir)
    assert again.path == result.path
    assert "<canvas" in again.path.read_text()
    assert not list((kb_dir / ".openkb/journal").glob("*.json"))
