"""Genuine model setup shared by application and retained-knowledge regressions."""

from dataclasses import dataclass, field

import pytest


@dataclass
class SemanticLintObservation:
    call_start: int
    reports: list[str] = field(default_factory=list)
    finish_reasons: list[str | None] = field(default_factory=list)


@pytest.fixture(autouse=True)
def real_application_model(request, monkeypatch, kb_dir):
    """Only live cases resolve credentials; offline preparation admits no sends."""
    if request.node.get_closest_marker("live") is not None:
        live_llm = request.getfixturevalue("live_llm")
        live_llm.install(monkeypatch)
        configure_application_model(kb_dir)
        return live_llm
    configure_application_model(kb_dir, max_calls=0)
    return None


def configure_application_model(kb_dir, *, max_calls=16):
    import yaml

    from ultrakb.config import save_config

    path = kb_dir / ".ultrakb/config.yaml"
    # Preserve configuration origin: persisting load_config's merged defaults
    # would turn inherited PDF/runtime policy into explicit KB overrides.
    config = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    config.update(model="deepseek/deepseek-flash", language="en")
    config.setdefault("model_policy", {}).update(max_calls=max_calls, retries=0)
    config.setdefault("role_generation", {}).setdefault("compile", {}).update(
        max_tokens=1024, reasoning_effort="none"
    )
    save_config(path, config)


def semantic_lint_fixture(kb_dir, live_llm, monkeypatch):
    """Use the same tiny genuine audit for CLI and REST delivery regressions."""
    import litellm
    from agents import Runner

    from ultrakb.config import load_config, save_config
    from ultrakb.locks import atomic_write_json, atomic_write_text

    live_llm.install(monkeypatch)
    path = kb_dir / ".ultrakb/config.yaml"
    config = load_config(path)
    config.update(model="deepseek/deepseek-flash", language="en")
    config["model_policy"] = {"max_calls": 5, "retries": 0}
    config.setdefault("role_generation", {})["answer"] = {
        "max_tokens": 1024,
        "reasoning_effort": "none",
    }
    save_config(path, config)
    atomic_write_json(
        kb_dir / ".ultrakb/hashes.json", {"abc123": {"name": "paper.pdf", "type": "pdf"}}
    )
    (kb_dir / "wiki/entities").mkdir(parents=True, exist_ok=True)
    atomic_write_text(kb_dir / "wiki/index.md", "# Index\n[[summaries/orion]]\n[[concepts/orion]]")
    atomic_write_text(
        kb_dir / "wiki/summaries/orion.md",
        "# Orion\nOrion costs 17 credits under standard conditions.\n"
        "See [[concepts/orion]] and Ghost Link.",
    )
    atomic_write_text(
        kb_dir / "wiki/concepts/orion.md",
        "# Orion\nOrion costs 23 credits under standard conditions.",
    )
    observation = SemanticLintObservation(call_start=len(live_llm.calls))
    original = Runner.run
    completion = litellm.acompletion

    async def observed_completion(**kwargs):
        response = await completion(**kwargs)
        observation.finish_reasons.append(response.choices[0].finish_reason)
        return response

    async def observed(*args, **kwargs):
        result = await original(*args, **kwargs)
        observation.reports.append(result.final_output)
        return result

    monkeypatch.setattr(litellm, "acompletion", observed_completion)
    monkeypatch.setattr(Runner, "run", observed)
    monkeypatch.setattr("ultrakb.agent.linter.MAX_TURNS", 5)
    return observation


def assert_semantic_lint_delivery(report, observation, live_llm):
    """Require a real, fact-grounded report to reach the application result."""
    assert observation.reports == [report]
    assert "orion" in report.lower() and "17" in report and "23" in report
    assert "Knowledge lint failed" not in report
    assert "No output produced" not in report
    assert 1 <= len(live_llm.calls) - observation.call_start <= 5
    assert observation.finish_reasons[-1] == "stop"
