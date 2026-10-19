"""Direct unit tests for :mod:`ultrakb.agent.skill_runner`.

Pre-existing test files mocked ``run_skill`` at every caller (deck
creator, generator, chat slash). That covered the dispatch boundary but
left the function itself untested. The tests below construct synthetic
``SKILL.md`` files under ``tmp_path`` and patch ``agents.Runner.run`` so
we can verify the assembly path end-to-end without spending tokens:

* skill lookup — ``SkillNotFoundError`` with a helpful "available" list
* prompt assembly — body lands in ``agent.instructions`` followed by
  the "## User intent" section
* tools wired — ``write_file`` + ``read_output_or_skill_file`` present
  alongside the inherited wiki-read tools
* output-path templating — ``{slug}`` substituted, injected into
  intent, file existence enforced post-run
* deck-mode validator hook — when frontmatter ``od.mode == "deck"``,
  ``validate_deck`` runs with the skill's grammar after the agent finishes
* MaxTurnsExceeded → RuntimeError translation with a useful message
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ultrakb.agent.skill_runner import (
    MAX_TURNS,
    SkillNotFoundError,
    SkillRunResult,
    run_skill,
)


def _make_kb(tmp_path: Path) -> Path:
    from kb_fixtures import mark_current_kb

    mark_current_kb(tmp_path)
    return tmp_path


def _install_skill(
    kb_dir: Path,
    name: str,
    *,
    body: str = "Do the thing.",
    od: dict | None = None,
    description: str = "A test skill.",
) -> Path:
    """Drop a SKILL.md under ``<kb>/skills/<name>/`` and return its path."""
    sk_dir = kb_dir / "skills" / name
    sk_dir.mkdir(parents=True)
    frontmatter = {"name": name, "description": description}
    if od is not None:
        frontmatter["od"] = od
    # Write YAML by hand to avoid a yaml.dump dependency on test side
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, dict):
            fm_lines.append(f"{k}:")
            for kk, vv in v.items():
                if isinstance(vv, dict):
                    fm_lines.append(f"  {kk}:")
                    for kkk, vvv in vv.items():
                        fm_lines.append(f"    {kkk}: {vvv!r}")
                else:
                    fm_lines.append(f"  {kk}: {vv!r}")
        else:
            fm_lines.append(f"{k}: {v!r}")
    fm_lines.append("---")
    (sk_dir / "SKILL.md").write_text("\n".join(fm_lines) + "\n" + body, encoding="utf-8")
    return sk_dir


@pytest.mark.asyncio
async def test_run_skill_raises_skill_not_found_with_available_list(tmp_path: Path):
    kb_dir = _make_kb(tmp_path)
    _install_skill(kb_dir, "alpha")
    _install_skill(kb_dir, "beta")

    with pytest.raises(SkillNotFoundError) as exc_info:
        await run_skill(
            skill_name="nonexistent",
            intent="anything",
            kb_dir=kb_dir,
            model="openai/gpt-4o",
        )
    msg = str(exc_info.value)
    # Helpful: lists what IS available so the user can see the typo
    assert "alpha" in msg
    assert "beta" in msg
    assert "nonexistent" in msg


@pytest.mark.asyncio
async def test_run_skill_loads_body_into_instructions(tmp_path: Path):
    from ultrakb.agent.skill_runner import finish_skill_run, prepare_skill_run

    kb_dir = _make_kb(tmp_path)
    _install_skill(kb_dir, "marker-skill", body="UNIQUE-BODY-MARKER")
    request = prepare_skill_run(
        skill_name="marker-skill",
        intent="DO-THE-INTENT",
        kb_dir=kb_dir,
        model="deepseek/deepseek-flash",
    )
    assert "UNIQUE-BODY-MARKER" in request.agent.instructions
    assert "DO-THE-INTENT" in request.agent.instructions
    assert "## User intent" in request.agent.instructions
    names = [tool.name for tool in request.agent.tools]
    assert "write_file" in names and "read_output_or_skill_file" in names
    result = finish_skill_run(request.definition)
    assert isinstance(result, SkillRunResult) and result.skill_name == "marker-skill"


@pytest.mark.asyncio
async def test_run_skill_templates_output_path_and_enforces_existence(tmp_path: Path):
    from ultrakb.agent.skill_runner import finish_skill_run, prepare_skill_run
    from ultrakb.locks import atomic_write_text

    kb_dir = _make_kb(tmp_path)
    _install_skill(
        kb_dir,
        "templated",
        od={"mode": "deck", "output_path_template": "output/decks/{slug}/index.html"},
    )
    request = prepare_skill_run(
        skill_name="templated",
        intent="brief",
        kb_dir=kb_dir,
        model="deepseek/deepseek-flash",
        slug="my-slug",
    )
    assert "output/decks/my-slug/index.html" in request.agent.instructions
    # Artifact data tests delivery/validation; it is not a claimed model response.
    atomic_write_text(
        request.definition.output_path, '<section class="slide" data-type="cover"></section>' * 8
    )
    result = finish_skill_run(request.definition)
    assert result.output_path is not None and result.output_path.name == "index.html"
    assert "my-slug" in str(result.output_path)


@pytest.mark.asyncio
async def test_run_skill_raises_if_templated_output_missing_post_run(tmp_path: Path):
    from ultrakb.agent.skill_runner import finish_skill_run, prepare_skill_run

    kb_dir = _make_kb(tmp_path)
    _install_skill(
        kb_dir,
        "lazy-skill",
        od={"mode": "deck", "output_path_template": "output/decks/{slug}/index.html"},
    )
    request = prepare_skill_run(
        skill_name="lazy-skill",
        intent="x",
        kb_dir=kb_dir,
        model="deepseek/deepseek-flash",
        slug="demo",
    )
    with pytest.raises(RuntimeError, match="did not write the expected"):
        finish_skill_run(request.definition)


@pytest.mark.asyncio
async def test_run_skill_runs_deck_validator_when_mode_is_deck(tmp_path: Path):
    from ultrakb.agent.skill_runner import finish_skill_run, prepare_skill_run
    from ultrakb.locks import atomic_write_text

    kb_dir = _make_kb(tmp_path)
    _install_skill(
        kb_dir,
        "deck-with-grammar",
        od={
            "mode": "deck",
            "output_path_template": "output/decks/{slug}/index.html",
            "deck_grammar": {"kind_attr": "data-type", "required": ["cover", "closing"]},
        },
    )
    request = prepare_skill_run(
        skill_name="deck-with-grammar",
        intent="x",
        kb_dir=kb_dir,
        model="deepseek/deepseek-flash",
        slug="demo",
    )
    atomic_write_text(
        request.definition.output_path,
        '<section class="slide" data-type="cover"></section>'
        + '<section class="slide" data-type="thesis"></section>' * 4,
    )
    result = finish_skill_run(request.definition)
    assert result.validation is not None
    assert any("closing" in error for error in result.validation.errors)


@pytest.mark.asyncio
async def test_run_skill_no_validator_when_mode_not_deck(tmp_path: Path):
    from ultrakb.agent.skill_runner import finish_skill_run, prepare_skill_run

    kb_dir = _make_kb(tmp_path)
    _install_skill(kb_dir, "no-mode-skill")
    request = prepare_skill_run(
        skill_name="no-mode-skill", intent="x", kb_dir=kb_dir, model="deepseek/deepseek-flash"
    )
    assert finish_skill_run(request.definition).validation is None


@pytest.mark.asyncio
async def test_run_skill_translates_max_turns_exceeded(tmp_path: Path):
    kb_dir = _make_kb(tmp_path)
    _install_skill(kb_dir, "loopy")
    with pytest.raises(RuntimeError) as caught:
        await run_skill(
            skill_name="loopy",
            intent="x",
            kb_dir=kb_dir,
            model="deepseek/deepseek-flash",
            max_turns=0,
        )
    message = str(caught.value)
    assert "0" in message and "step" in message.lower() and "loopy" in message


@pytest.mark.asyncio
async def test_run_skill_default_max_turns(tmp_path: Path):
    from ultrakb.agent.skill_runner import prepare_skill_run

    kb_dir = _make_kb(tmp_path)
    _install_skill(kb_dir, "default-budget")
    request = prepare_skill_run(
        skill_name="default-budget", intent="x", kb_dir=kb_dir, model="deepseek/deepseek-flash"
    )
    assert request.max_turns == MAX_TURNS


@pytest.mark.asyncio
async def test_real_skill_writes_and_validates_the_requested_artifact(
    tmp_path, live_llm, monkeypatch
):
    live_llm.install(monkeypatch)
    kb_dir = _make_kb(tmp_path)
    _install_skill(
        kb_dir,
        "real-note",
        body=(
            "Write a short Markdown note using only the supplied user facts. "
            "Write the whole output once with write_file. No wiki search is needed."
        ),
        od={"mode": "note", "output_path_template": "output/notes/{slug}.md"},
    )
    result = await run_skill(
        skill_name="real-note",
        intent="Supplied fact: Orion costs 17 credits.",
        kb_dir=kb_dir,
        model="deepseek/deepseek-flash",
        bundle=live_llm.credentials,
        slug="orion",
        max_turns=4,
    )
    assert result.output_path.is_file()
    text = result.output_path.read_text()
    assert "Orion" in text and "17" in text
    assert result.validation is None and result.metadata["mode"] == "note"
