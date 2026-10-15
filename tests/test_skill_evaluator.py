"""Tests for ultrakb.skill.evaluator.

The Runner.run call is mocked everywhere — no real LLM tokens spent.
What we DO verify:
  * Description extraction from SKILL.md frontmatter
  * Generator output parsing (with + without code fences)
  * Grader response handling (uppercase / lowercase / ambiguous)
  * End-to-end run_eval with mocked grading
  * Save/load round-trip for persisted eval sets
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ultrakb.skill.evaluator import (
    EvalPrompt,
    EvalResult,
    _read_description,
    assemble_eval_result,
    build_coverage_grader,
    build_eval_set_agent,
    build_trigger_grader,
    generate_eval_set,
    grade_coverage,
    grade_one,
    load_eval_set,
    parse_coverage_verdict,
    parse_eval_set,
    parse_trigger_verdict,
    save_eval_set,
)


def _make_skill(tmp_path: Path, *, description: str | None = "Triggers for foo questions.") -> Path:
    """Create a minimal SKILL.md and return the skill directory."""
    skill_dir = tmp_path / "output" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    if description is None:
        body = "---\nname: demo\n---\n\n# demo\n"
    else:
        body = f"---\nname: demo\ndescription: {description}\n---\n\n# demo\n"
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


# -------- _read_description ---------------------------------------------------


def test_read_description_extracts_field(tmp_path):
    skill_dir = _make_skill(tmp_path, description="Distill thoughts about transformers.")
    assert _read_description(skill_dir) == "Distill thoughts about transformers."


def test_read_description_raises_on_missing_frontmatter(tmp_path):
    skill_dir = tmp_path / "output" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# demo\n\nNo frontmatter at all.\n")
    with pytest.raises(RuntimeError, match="frontmatter"):
        _read_description(skill_dir)


def test_read_description_raises_on_missing_description_field(tmp_path):
    skill_dir = _make_skill(tmp_path, description=None)
    with pytest.raises(RuntimeError, match="description"):
        _read_description(skill_dir)


# -------- generate_eval_set ---------------------------------------------------


def _eval_protocol_payload(count: int = 10) -> str:
    trig = [f"trigger question {i}" for i in range(count)]
    no = [f"unrelated question {i}" for i in range(count)]
    return json.dumps({"should_trigger": trig, "should_not": no})


@pytest.mark.asyncio
async def test_generate_eval_set_parses_plain_json(tmp_path, live_llm, monkeypatch):
    live_llm.install(monkeypatch)
    skill_dir = _make_skill(tmp_path, description="Use for choosing a quiet reading room.")
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Use for choosing a quiet reading room.\n---\n"
        "# Rooms\nThe indoor room and garden room both permit quiet reading.\n"
    )
    prompts = await generate_eval_set(skill_dir, model="deepseek/deepseek-flash", count=2)
    assert len(prompts) == 4
    assert sum(p.expected == "trigger" for p in prompts) == 2
    assert sum(p.expected == "no-trigger" for p in prompts) == 2
    assert all(p.question.strip() for p in prompts)
    assert any("room" in p.question.lower() for p in prompts if p.expected == "trigger")


@pytest.mark.asyncio
async def test_generate_eval_set_strips_code_fences(tmp_path):
    fenced = "```json\n" + _eval_protocol_payload(3) + "\n```"
    prompts = parse_eval_set(fenced)
    assert len(prompts) == 6
    assert [p.question for p in prompts[:3]] == [
        "trigger question 0",
        "trigger question 1",
        "trigger question 2",
    ]


# -------- grade_one -----------------------------------------------------------


@pytest.mark.asyncio
async def test_grade_one_returns_trigger_for_trigger_response(live_llm, monkeypatch):
    live_llm.install(monkeypatch)
    out = await grade_one(
        "Use for choosing a quiet reading room.",
        "Which room should I choose for quiet reading?",
        model="deepseek/deepseek-flash",
    )
    assert out == "trigger"


@pytest.mark.asyncio
async def test_grade_one_returns_no_trigger_for_negative_response(live_llm, monkeypatch):
    live_llm.install(monkeypatch)
    out = await grade_one(
        "Use for choosing a quiet reading room.",
        "What is the weather in London?",
        model="deepseek/deepseek-flash",
    )
    assert out == "no-trigger"


@pytest.mark.asyncio
async def test_grade_one_handles_mixed_case():
    assert parse_trigger_verdict("trigger") == "trigger"


@pytest.mark.asyncio
async def test_grade_one_handles_space_variant():
    assert parse_trigger_verdict("No Trigger") == "no-trigger"


@pytest.mark.asyncio
async def test_grade_one_defaults_to_no_trigger_on_ambiguous_output():
    assert parse_trigger_verdict("hmm not sure") == "no-trigger"


# -------- run_eval ------------------------------------------------------------


def _build_eval_set(n_trig: int = 3, n_no: int = 3) -> list[EvalPrompt]:
    prompts: list[EvalPrompt] = []
    for i in range(n_trig):
        prompts.append(EvalPrompt(question=f"trig {i}", expected="trigger"))
    for i in range(n_no):
        prompts.append(EvalPrompt(question=f"no {i}", expected="no-trigger"))
    return prompts


@pytest.mark.asyncio
async def test_run_eval_happy_path_all_correct(tmp_path):
    eval_set = _build_eval_set(3, 3)
    result = assemble_eval_result(eval_set, [p.expected for p in eval_set], [("supported", "")] * 3)
    assert isinstance(result, EvalResult)
    assert result.total == 6
    assert result.passed == 6
    assert result.pass_rate == 1.0
    assert result.misses == []
    assert result.trigger_questions == 3
    assert result.coverage_passed == 3
    assert result.coverage_rate == 1.0
    assert result.coverage_misses == []


@pytest.mark.asyncio
async def test_run_eval_reports_misses(tmp_path):
    eval_set = _build_eval_set(3, 3)
    result = assemble_eval_result(eval_set, ["trigger"] * 6, [("supported", "")] * 3)
    assert result.total == 6
    assert result.passed == 3
    assert len(result.misses) == 3
    assert all((m.prompt.expected == "no-trigger" for m in result.misses))
    assert all((m.graded == "trigger" for m in result.misses))
    assert result.pass_rate == pytest.approx(0.5)
    assert "no-trigger" in result.misses[0].label
    assert "trigger" in result.misses[0].label


@pytest.mark.asyncio
async def test_run_eval_reports_coverage_gaps(tmp_path):
    """A correct activation decision cannot hide unsupported body content."""
    eval_set = _build_eval_set(3, 3)
    result = assemble_eval_result(
        eval_set,
        [p.expected for p in eval_set],
        [("unsupported", "body has no material")] * 2 + [("supported", "")],
    )
    assert result.passed == 6
    assert result.trigger_questions == 3
    assert len(result.coverage_misses) == 2
    assert {g.prompt.question for g in result.coverage_misses} == {"trig 0", "trig 1"}
    assert all((g.reason == "body has no material" for g in result.coverage_misses))
    assert result.coverage_rate == pytest.approx(1 / 3)


@pytest.mark.asyncio
async def test_grade_coverage_parses_supported_verdict(live_llm, monkeypatch):
    live_llm.install(monkeypatch)
    verdict, reason = await grade_coverage(
        "The indoor room and garden room both permit quiet reading.",
        "Which rooms permit quiet reading?",
        model="deepseek/deepseek-flash",
    )
    assert verdict == "supported"
    assert reason.strip()


@pytest.mark.asyncio
async def test_grade_coverage_reports_ambiguous_on_unparseable_output():
    verdict, reason = parse_coverage_verdict("hmm not sure")
    assert verdict == "ambiguous"
    assert "unparseable grader output" in reason


@pytest.mark.asyncio
async def test_run_eval_segregates_ambiguous_from_coverage_misses(tmp_path):
    """Ambiguous coverage is kept separate and excluded from the scored denominator."""
    eval_set = _build_eval_set(3, 0)
    result = assemble_eval_result(
        eval_set,
        ["trigger"] * 3,
        [
            ("supported", ""),
            ("unsupported", "body gap"),
            ("ambiguous", "unparseable grader output: xxx"),
        ],
    )
    assert result.trigger_questions == 3
    assert len(result.coverage_misses) == 1
    assert len(result.coverage_ambiguous) == 1
    assert result.coverage_passed == 1
    assert result.coverage_rate == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_run_eval_captures_grader_failures_without_aborting(tmp_path):
    """One failed classification cannot discard other completed classifications."""
    eval_set = _build_eval_set(2, 2)
    result = assemble_eval_result(
        eval_set,
        [RuntimeError("max-turn cap hit"), "trigger", "no-trigger", "no-trigger"],
        [("supported", ""), RuntimeError("malformed grader output")],
    )
    assert result.total == 4
    assert len(result.trigger_errors) == 1
    assert result.trigger_errors[0].prompt.question == "trig 0"
    assert "max-turn cap" in result.trigger_errors[0].reason
    assert len(result.coverage_errors) == 1
    assert result.coverage_errors[0].prompt.question == "trig 1"
    assert "malformed" in result.coverage_errors[0].reason
    assert result.trigger_scored == 3
    assert result.passed == 3
    assert result.pass_rate == pytest.approx(1.0)
    assert result.coverage_passed == 1
    assert result.coverage_rate == pytest.approx(1.0)


# -------- save/load round-trip ------------------------------------------------


def test_save_and_load_eval_set_round_trip(tmp_path):
    prompts = _build_eval_set(2, 2)
    path = save_eval_set(tmp_path, "demo", prompts)

    assert path == tmp_path / ".ultrakb" / "eval-sets" / "demo.json"
    assert path.is_file()

    data = json.loads(path.read_text())
    assert data["should_trigger"] == ["trig 0", "trig 1"]
    assert data["should_not"] == ["no 0", "no 1"]

    loaded = load_eval_set(path)
    assert len(loaded) == 4
    assert [p.question for p in loaded if p.expected == "trigger"] == ["trig 0", "trig 1"]
    assert [p.question for p in loaded if p.expected == "no-trigger"] == ["no 0", "no 1"]


# -------- RuntimeError translation for CLI catch -------------------------------


@pytest.mark.asyncio
async def test_generate_eval_set_translates_max_turns_to_runtime_error(tmp_path):
    skill_dir = _make_skill(tmp_path)
    with pytest.raises(RuntimeError, match="max-turn cap"):
        await generate_eval_set(skill_dir, model="deepseek/deepseek-flash", max_turns=0)


@pytest.mark.asyncio
async def test_generate_eval_set_translates_malformed_json_to_runtime_error(tmp_path):
    with pytest.raises(RuntimeError, match="non-JSON output"):
        parse_eval_set("this is not json at all")


# -------- parallel_tool_calls on the (tool-less) eval agents ------------------
#
# The eval agents have NO tools, so they must OMIT parallel_tool_calls in every
# config state — the SDK forwards an explicit `False` even without tools, which
# strict OpenAI endpoints reject. Regression guards against a future "finish the
# migration" that wires them through resolve_model_settings().


async def _all_three_eval_agents(skill_dir):
    return [
        build_eval_set_agent(skill_dir, model="deepseek/deepseek-flash", count=2),
        build_trigger_grader("desc", model="deepseek/deepseek-flash"),
        build_coverage_grader("body", model="deepseek/deepseek-flash"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stash",
    [
        (None, False),  # not configured
        (None, True),  # explicit null (Bedrock escape hatch)
        (False, True),  # explicit false — must NOT reach a tool-less agent as False
        (True, True),  # explicit true — meaningless without tools
    ],
)
async def test_eval_agents_always_omit_parallel_tool_calls(tmp_path, stash):
    from ultrakb.config import set_parallel_tool_calls

    skill_dir = _make_skill(tmp_path)
    set_parallel_tool_calls(*stash)

    for agent in await _all_three_eval_agents(skill_dir):
        assert agent.model_settings.parallel_tool_calls is None
