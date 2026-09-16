"""Saved artifact evidence through shared generation, reading and lifecycle entry points."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from agents import Runner
from agents.tool_context import ToolContext

from openkb.application.artifacts import artifact_quality
from openkb.application.documents import import_document
from openkb.application.generators import GenerationOptions, generate_artifact


@pytest.fixture
def source_kb(kb_dir, tmp_path, model_service):
    # A byte-identical copy of allowed D09, used only with the local protocol fixture.
    original = tmp_path / "calibration-text.md"
    original.write_text(
        "# Calibration: AX-4 controller\n\n"
        "Set AX-4 hold time to 14 seconds only after stopping the sampling job.\n\n"
        "Do not restart automatically after an authentication failure.\n\n"
        "Exception: in maintenance mode, collect one diagnostic record without restarting.\n"
    )
    result = import_document(kb_dir, original)
    assert result.status == "added", result
    return kb_dir


async def invoke(agent, name, **arguments):
    tool = next(tool for tool in agent.tools if tool.name == name)
    data = json.dumps(arguments)
    context = ToolContext(None, tool_name=name, tool_call_id=name, tool_arguments=data)
    return await tool.on_invoke_tool(context, data)


async def original_row(agent):
    listing = json.loads(await invoke(agent, "list_sources", offset=0, limit=20))
    source = listing["sources"][0]["source_id"]
    tree = json.loads(await invoke(agent, "read_source_tree", source_id=source, offset=0, limit=20))
    result = json.loads(
        await invoke(
            agent,
            "read_source_node",
            source_id=source,
            node_id=tree["nodes"][0]["id"],
            offset=0,
            start=0,
            max_chars=16000,
        )
    )
    return result["evidence"][-1]


def test_uncited_saved_artifact_does_not_claim_citation_success(source_kb, monkeypatch):
    async def produce(agent, *args, **kwargs):
        await original_row(agent)
        await invoke(
            agent,
            "write_skill_file",
            path="SKILL.md",
            content="# Installation guidance\nA paraphrase without a source link.\n",
        )
        return SimpleNamespace(final_output="Saved")

    monkeypatch.setattr(Runner, "run", produce)
    result = asyncio.run(generate_artifact(source_kb, GenerationOptions("skill", "demo", "Cite")))
    assert result.status == "completed"
    quality = artifact_quality(source_kb, "output/skills/demo")
    assert quality["references"] == []
    assert quality["checks"]["citations"] == "not_checked"
    assert "source_citations" in quality["unchecked"]

    # Earlier v1 records marked a citation-free file as passed. Reading those
    # records must remain honest without rewriting their saved bytes or digest.
    from openkb.artifact_quality import quality_path
    from openkb.sources import content_id

    path = quality_path(source_kb, source_kb / "output/skills/demo")
    record = json.loads(path.read_text())
    record.pop("digest")
    record["checks"]["citations"] = "passed"
    record["unchecked"].remove("source_citations")
    record["digest"] = content_id(record)
    path.write_text(json.dumps(record))
    original_bytes = path.read_bytes()
    restored = artifact_quality(source_kb, "output/skills/demo")
    assert restored["checks"]["citations"] == "not_checked"
    assert "source_citations" in restored["unchecked"]
    assert path.read_bytes() == original_bytes


def test_saved_markdown_cites_only_observed_version_and_keeps_examples(source_kb, monkeypatch):
    observed = []

    async def produce(agent, *args, **kwargs):
        row = await original_row(agent)
        observed.append(row)
        marker = row["short_citation"]
        await invoke(
            agent,
            "write_skill_file",
            path="SKILL.md",
            content=(
                "---\nname: demo\ndescription: Original evidence\n---\n"
                f"Read original {marker}\n\n`{marker}`\n\n```md\n{marker}\n```\n"
            ),
        )
        return SimpleNamespace(final_output="Done")

    monkeypatch.setattr(Runner, "run", produce)
    result = asyncio.run(generate_artifact(source_kb, GenerationOptions("skill", "demo", "Cite")))
    assert result.status == "completed", result
    quality = artifact_quality(source_kb, "output/skills/demo")
    assert quality["checks"]["citations"] == "passed", quality
    text = (source_kb / "output/skills/demo/SKILL.md").read_text()
    row = observed[0]
    assert text.count(row["short_citation"]) == 2
    assert "../../../wiki/sources/snapshots/" in text
    assert quality["references"][0]["reference"] == row["reference"]


def test_saved_html_expands_prose_citations_but_preserves_scripts_and_code(source_kb, monkeypatch):
    definition = source_kb / "skills/custom/SKILL.md"
    definition.parent.mkdir(parents=True)
    definition.write_text(
        "---\nname: custom\ndescription: Test HTML\nod:\n  mode: deck\n"
        "  output_path_template: output/decks/{slug}/index.html\n---\nMake a deck."
    )
    original = []

    async def produce(agent, *args, **kwargs):
        row = await original_row(agent)
        marker = row["short_citation"]
        literal = (
            f'<script>const example = "{marker}";</script>'
            f'<pre><code>{marker}<a href="sources/example.md">example</a></code></pre>'
            "<!-- [evidence:comment] -->"
        )
        original.append(literal)
        await invoke(
            agent,
            "write_file",
            path="output/decks/demo/index.html",
            content=(
                f"<!doctype html><html><body><p>Source {marker}</p>"
                "<p>Unknown [evidence:unknown]</p>" + literal + "</body></html>"
            ),
        )
        return SimpleNamespace(final_output="Done")

    monkeypatch.setattr(Runner, "run", produce)
    result = asyncio.run(
        generate_artifact(
            source_kb,
            GenerationOptions(
                "deck",
                "demo",
                "Cite original",
                skill_name="custom",
            ),
        )
    )
    assert result.status == "completed", result
    text = (source_kb / "output/decks/demo/index.html").read_text()
    assert '<p>Source <a href="../../../wiki/sources/snapshots/' in text
    assert original[0] in text
    quality = artifact_quality(source_kb, "output/decks/demo")
    assert quality["checks"]["citations"] == "failed"
    assert any("unknown" in issue for issue in quality["issues"])
    assert not any("example" in issue or "comment" in issue for issue in quality["issues"])


@pytest.mark.parametrize("folder", ["output/decks", "wiki/explorations"])
def test_replacement_deck_checks_new_support_files_without_rechecking_history(
    source_kb, monkeypatch, folder
):
    import io
    import zipfile

    from openkb.application.artifacts import artifact_archive

    definition = source_kb / "skills/custom/SKILL.md"
    definition.parent.mkdir(parents=True)
    definition.write_text(
        "---\nname: custom\ndescription: Test HTML\nod:\n  mode: deck\n"
        f"  output_path_template: {folder}/{{slug}}/index.html\n---\nMake a deck."
    )
    relative = f"{folder}/demo"
    calls = []
    old_html = "<html><body>Old [evidence:unknown]</body></html>"

    async def produce(agent, *args, **kwargs):
        content = old_html
        if calls:
            row = await original_row(agent)
            content = f"<html><body>New {row['short_citation']}</body></html>"
            await invoke(
                agent, "write_file", path="output/support.md", content=row["short_citation"]
            )
        calls.append(content)
        await invoke(agent, "write_file", path=f"{relative}/index.html", content=content)
        return SimpleNamespace(final_output="Saved")

    monkeypatch.setattr(Runner, "run", produce)
    for _ in range(2):
        result = asyncio.run(
            generate_artifact(
                source_kb,
                GenerationOptions("deck", "demo", "Cite", overwrite="archive", skill_name="custom"),
            )
        )
        assert result.status == "completed", result
    quality = artifact_quality(source_kb, relative)
    assert quality["checks"]["citations"] == "passed", quality
    assert set(quality["files"]) == {f"{relative}/index.html", "output/support.md"}
    archived = result.archive_path.relative_to(source_kb).as_posix()
    assert (result.archive_path / "index.html").read_text() == old_html
    assert artifact_quality(source_kb, archived)["checks"]["citations"] == "failed"
    with zipfile.ZipFile(
        io.BytesIO(artifact_archive(source_kb, relative, include_evidence=True))
    ) as package:
        assert not any("-workspace/" in name for name in package.namelist())
        assert any(name.endswith("support.md") for name in package.namelist())


def test_archived_artifact_keeps_quality_and_is_a_source_retention_root(source_kb, monkeypatch):
    from openkb.application.removal import preview_removal
    from openkb.application.source_cleanup import preview_history_cleanup

    rows = []

    async def produce(agent, *args, **kwargs):
        content = "# Replacement without source claims"
        if not rows:
            row = await original_row(agent)
            rows.append(row)
            content = "# Original\n" + row["short_citation"]
        await invoke(agent, "write_skill_file", path="SKILL.md", content=content)
        return SimpleNamespace(final_output="Saved")

    monkeypatch.setattr(Runner, "run", produce)
    first = asyncio.run(generate_artifact(source_kb, GenerationOptions("skill", "demo", "Cite")))
    assert first.status == "completed"
    second = asyncio.run(
        generate_artifact(
            source_kb,
            GenerationOptions(
                "skill",
                "demo",
                "Replace",
                overwrite="archive",
            ),
        )
    )
    assert second.status == "completed", second
    archived = second.archive_path.relative_to(source_kb).as_posix()
    quality = artifact_quality(source_kb, archived)
    assert quality["checks"]["citations"] == "passed", quality
    assert "../../../../wiki/sources/" in (second.archive_path / "SKILL.md").read_text()
    preview = preview_removal(source_kb, "calibration-text.md")
    assert any(archived in action.target for action in preview.plan.actions)
    cleanup = preview_history_cleanup(source_kb)
    assert archived in cleanup.artifact_dependencies
    assert rows[0]["reference"]["version_id"] not in cleanup.versions
    import zipfile

    from openkb.application.artifacts import export_artifact, list_artifacts

    assert archived in {item.path for item in list_artifacts(source_kb)}
    destination = source_kb.parent / (source_kb.name + "-archive-export")
    destination.mkdir()
    exported = export_artifact(source_kb, archived, destination, include_evidence=True)
    with zipfile.ZipFile(exported) as package:
        assert "_evidence/sources.html#" in package.read("iteration-1/SKILL.md").decode()


def test_portable_export_contains_only_cited_original_ranges_and_local_links(
    source_kb, monkeypatch
):
    import zipfile

    from openkb.application.artifacts import export_artifact

    rows = []

    async def produce(agent, *args, **kwargs):
        row = await original_row(agent)
        rows.append(row)
        await invoke(
            agent,
            "write_skill_file",
            path="SKILL.md",
            content=("# Cited original\n" + row["short_citation"]),
        )
        await invoke(
            agent, "write_skill_file", path="references/usage.md", content="Use the skill."
        )
        return SimpleNamespace(final_output="Saved")

    monkeypatch.setattr(Runner, "run", produce)
    result = asyncio.run(generate_artifact(source_kb, GenerationOptions("skill", "demo", "Cite")))
    assert result.status == "completed"
    destination = source_kb.parent / (source_kb.name + "-export")
    destination.mkdir()
    exported = export_artifact(source_kb, "output/skills/demo", destination, include_evidence=True)
    with zipfile.ZipFile(exported) as archive:
        text = archive.read("demo/SKILL.md").decode()
        assert "../../../wiki" not in text
        assert "_evidence/sources.html#" in text
        sources = archive.read("demo/_evidence/sources.html").decode()
        assert rows[0]["text"] in sources
        assert "Set AX-4 hold time" not in sources  # That block was not cited.
        assert archive.read("demo/references/usage.md") == b"Use the skill."
        assert not any(".env" in name or "source-store" in name for name in archive.namelist())


def test_rollback_restores_quality_and_deletion_releases_only_selected_roots(
    source_kb, monkeypatch
):
    from openkb.application.artifacts import delete_artifact
    from openkb.application.skill_maintenance import rollback_skill
    from openkb.application.source_cleanup import preview_history_cleanup

    versions = []

    async def produce(agent, *args, **kwargs):
        row = await original_row(agent)
        content = f"# Version {len(versions)}\n" + row["short_citation"]
        versions.append(content)
        await invoke(agent, "write_skill_file", path="SKILL.md", content=content)
        return SimpleNamespace(final_output="Saved")

    monkeypatch.setattr(Runner, "run", produce)
    for _ in range(2):
        result = asyncio.run(
            generate_artifact(
                source_kb, GenerationOptions("skill", "demo", "Cite", overwrite="archive")
            )
        )
        assert result.status == "completed"
    restored = rollback_skill(source_kb, "demo", iteration=1)
    assert "Version 0" in (restored / "SKILL.md").read_text()
    assert artifact_quality(source_kb, "output/skills/demo")["checks"]["citations"] == "passed"
    delete_artifact(source_kb, "output/skills/demo-workspace")
    roots = preview_history_cleanup(source_kb).artifact_dependencies
    assert "output/skills/demo" in roots
    assert not any("demo-workspace" in root for root in roots)


def test_truncation_preserves_saved_files_and_marks_execution_incomplete(source_kb, monkeypatch):
    from openkb.processing import OutputTruncated

    async def produce(agent, *args, **kwargs):
        await invoke(agent, "write_skill_file", path="SKILL.md", content="# Partial saved work")
        raise OutputTruncated("generation")

    monkeypatch.setattr(Runner, "run", produce)
    result = asyncio.run(generate_artifact(source_kb, GenerationOptions("skill", "demo", "Cite")))
    assert result.status == "failed"
    assert result.unfinished == ("generation",)
    assert (result.output_dir / "SKILL.md").read_text() == "# Partial saved work"


def test_markdown_html_literals_and_unobserved_source_links(source_kb, monkeypatch):
    async def produce(agent, *args, **kwargs):
        row = await original_row(agent)
        content = (
            f"# Evidence\n{row['short_citation']}\n\n"
            "Inline <code>[evidence:literal]</code> example.\n\n"
            '<a href="file:///another-kb/wiki/sources/snapshots/invalid.md#block-wrong">wrong</a>\n'
            '<script>let x = "[evidence:script]";</script>\n'
        )
        await invoke(agent, "write_skill_file", path="SKILL.md", content=content)
        return SimpleNamespace(final_output="Saved")

    monkeypatch.setattr(Runner, "run", produce)
    result = asyncio.run(generate_artifact(source_kb, GenerationOptions("skill", "demo", "Cite")))
    quality = artifact_quality(source_kb, "output/skills/demo")
    assert quality["checks"]["citations"] == "failed"
    assert any("another-kb" in issue for issue in quality["issues"])
    assert not any("literal" in issue or "script" in issue for issue in quality["issues"])
    assert "<code>[evidence:literal]</code>" in (result.output_dir / "SKILL.md").read_text()


def test_corrupt_record_blocks_cleanup_and_missing_snapshot_invalidates_checks(
    source_kb, monkeypatch
):
    from openkb.application.source_cleanup import preview_history_cleanup
    from openkb.artifact_quality import quality_path

    async def produce(agent, *args, **kwargs):
        row = await original_row(agent)
        await invoke(agent, "write_skill_file", path="SKILL.md", content=row["short_citation"])
        return SimpleNamespace(final_output="Saved")

    monkeypatch.setattr(Runner, "run", produce)
    result = asyncio.run(generate_artifact(source_kb, GenerationOptions("skill", "demo", "Cite")))
    record = artifact_quality(source_kb, "output/skills/demo")
    target = source_kb / "wiki" / record["references"][0]["target"].split("#")[0]
    target.unlink()
    assert artifact_quality(source_kb, "output/skills/demo")["status"] == "stale"
    quality_path(source_kb, result.output_dir).write_text('{"artifact":"output/skills/demo"}')
    assert artifact_quality(source_kb, "output/skills/demo")["status"] == "unknown"
    with pytest.raises(ValueError, match="quality history needs repair"):
        preview_history_cleanup(source_kb)
    from openkb.application.artifacts import delete_artifact

    delete_artifact(source_kb, "output/skills/demo")
    assert not result.output_dir.exists()


def test_api_returns_partial_files_and_current_unknown_or_stale_checks(source_kb, monkeypatch):
    from fastapi.testclient import TestClient

    from openkb.api import create_app

    async def produce(agent, *args, **kwargs):
        await invoke(agent, "write_skill_file", path="SKILL.md", content="# Partial")
        raise ConnectionError("injected provider failure")

    monkeypatch.setattr(Runner, "run", produce)
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda value: source_kb)
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/skill",
        json={
            "kb": "test",
            "name": "demo",
            "intent": "Cite",
            "stream": False,
        },
    )
    assert response.status_code == 500
    result = response.json()
    assert result["execution"] == "failed"
    assert any(path.endswith("SKILL.md") for path in result["resources"])
    assert result["artifact_quality"]["checks"]["semantics"] == "not_checked"
    (source_kb / "output/skills/demo/SKILL.md").write_text("# Edited")
    quality = client.get(
        "/api/v1/artifacts/quality", params={"kb": "test", "path": "output/skills/demo"}
    )
    assert quality.json()["status"] == "stale"


def test_failure_before_first_write_remains_deletable(source_kb, monkeypatch):
    from openkb.application.artifacts import delete_artifact

    async def fail(*args, **kwargs):
        raise ConnectionError("before first write")

    monkeypatch.setattr(Runner, "run", fail)
    result = asyncio.run(generate_artifact(source_kb, GenerationOptions("skill", "demo", "Cite")))
    assert result.status == "failed"
    assert artifact_quality(source_kb, "output/skills/demo")["status"] == "unknown"
    delete_artifact(source_kb, "output/skills/demo")
    assert not result.output_dir.exists()


@pytest.mark.parametrize("zone", ["output/skills/demo", "wiki/explorations"])
def test_unrecorded_partial_markers_conservatively_keep_history(source_kb, monkeypatch, zone):
    from openkb.application.source_cleanup import preview_history_cleanup

    async def produce(agent, *args, **kwargs):
        row = await original_row(agent)
        await invoke(agent, "write_skill_file", path="SKILL.md", content=row["short_citation"])
        return SimpleNamespace(final_output="Saved")

    def cannot_record(*args, **kwargs):
        raise OSError("quality storage failed")

    monkeypatch.setattr(Runner, "run", produce)
    monkeypatch.setattr("openkb.application.generators.save_quality", cannot_record)
    result = asyncio.run(generate_artifact(source_kb, GenerationOptions("skill", "demo", "Cite")))
    assert result.status == "failed"
    if zone == "wiki/explorations":
        from openkb.locks import kb_ingest_lock
        from openkb.mutation import mutation_scope

        target = source_kb / zone / "SKILL.md"
        original = source_kb / "output/skills/demo/SKILL.md"
        with kb_ingest_lock(source_kb / ".openkb"):
            with mutation_scope(source_kb, [original, target], operation="relocate test artifact"):
                target.parent.mkdir(parents=True, exist_ok=True)
                original.rename(target)
    preview = preview_history_cleanup(source_kb)
    assert not preview.versions and not preview.parses
    assert f"{zone}/SKILL.md" in preview.artifact_dependencies
