"""Business outcomes agree across adapters, using the desktop use cases as reference."""

import asyncio
import importlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_DIR", profile)
    monkeypatch.setattr("openkb.config.GLOBAL_CONFIG_PATH", profile / "global.yaml")
    monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)


def api_client(kb_dir, monkeypatch):
    from openkb.api import create_app

    monkeypatch.setattr("openkb.api_helpers.resolve_kb_alias", lambda _: kb_dir)
    return TestClient(create_app())


@pytest.mark.parametrize("entry", ["cli", "api", "api_stream", "desktop"])
def test_question_wording_never_requires_product_alias_confirmation(
    kb_dir, monkeypatch, narrated_model, entry
):
    from openkb.application.conversations import ask_question

    question = "What does Atlas V99 say about the answer?"
    if entry == "cli":
        cli = importlib.import_module("openkb.cli")
        monkeypatch.setattr(cli, "_setup_llm_key", lambda _: None)
        result = CliRunner().invoke(cli.cli, ["--kb-dir", str(kb_dir), "query", question])
        assert result.exit_code == 0, result.output
        body = result.output
    elif entry.startswith("api"):
        with api_client(kb_dir, monkeypatch) as client:
            result = client.post(
                "/api/v1/query",
                json={"kb": "audit", "question": question, "stream": entry == "api_stream"},
            )
        assert result.status_code == 200, result.text
        body = result.text
        if entry == "api":
            assert result.json()["answer_outcome"] == "answered"
            assert not result.json().get("scope_candidates")
    else:
        result = asyncio.run(ask_question(kb_dir, question))
        assert result.answer_outcome == "answered" and not result.scope_candidates
        body = result.answer
    assert "The answer is 42." in body


@pytest.mark.parametrize("entry", ["cli", "api", "desktop"])
def test_import_batch_keeps_initial_model(kb_dir, monkeypatch, entry):
    from openkb.application.documents import import_document
    from openkb.application.execution import ExecutionContext

    settings = kb_dir / ".openkb/config.yaml"
    settings.write_text("model: openai/initial\nlanguage: en\n")
    incoming = kb_dir / "incoming"
    incoming.mkdir()
    paths = [incoming / "alpha.md", incoming / "beta.md"]
    for path in paths:
        path.write_text(f"# {path.stem}\nDistinct source {path.stem}.")
    models = []

    def completion(**kwargs):
        models.append(kwargs["model"])
        if len(models) == 2:
            settings.write_text("model: openai/changed\nlanguage: zh\n")
        payload = (
            {"description": "Fixture", "content": "# Fixture\nCompiled."}
            if len(models) % 2
            else {"create": [], "update": [], "related": []}
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr("litellm.completion", completion)
    if entry == "cli":
        cli = importlib.import_module("openkb.cli")
        monkeypatch.setattr(cli, "_setup_llm_key", lambda _: None)
        result = CliRunner().invoke(cli.cli, ["--kb-dir", str(kb_dir), "add", str(incoming)])
        assert result.exit_code == 0, result.output
    elif entry == "api":
        with api_client(kb_dir, monkeypatch) as client:
            result = client.post(
                "/api/v1/add",
                data={"kb": "audit", "stream": "false"},
                files=[("files", (p.name, p.read_bytes(), "text/markdown")) for p in paths],
            )
        assert result.status_code == 200, result.text
        assert result.json()["added_count"] == 2
    else:
        context = ExecutionContext()
        assert [import_document(kb_dir, p, context=context).status for p in paths] == [
            "added",
            "added",
        ]
    assert models == ["openai/initial"] * 4


@pytest.fixture
def narrated_model(kb_dir, monkeypatch, request):
    from agents import RawResponsesStreamEvent, Runner
    from openai.types.responses import ResponseTextDeltaEvent

    deltas, answer = getattr(
        request, "param", (("Let me search. ", "The answer is 42."), "The answer is 42.")
    )
    from evidence_model import read_fixture, review_response

    (kb_dir / "wiki/sources/fixture.md").write_text("The answer is 42. Final answer only.")

    class Run:
        final_output = answer
        is_complete = False

        def __init__(self, agent):
            self.agent = agent

        async def stream_events(self):
            await read_fixture(self.agent)
            for index, delta in enumerate(deltas):
                yield RawResponsesStreamEvent(
                    data=ResponseTextDeltaEvent(
                        type="response.output_text.delta",
                        delta=delta,
                        item_id="answer",
                        output_index=0,
                        content_index=0,
                        sequence_number=index,
                        logprobs=[],
                    )
                )
            self.is_complete = True

        def cancel(self, **kwargs):
            pass

        def to_input_list(self):
            return [{"role": "assistant", "content": self.final_output}]

    monkeypatch.setattr(Runner, "run_streamed", lambda agent, *a, **kw: Run(agent))

    async def run(agent, input, **kwargs):
        if agent.name == "evidence-review":
            return review_response(input)
        await read_fixture(agent)
        return Run(agent)

    monkeypatch.setattr(Runner, "run", run)


@pytest.mark.parametrize("entry", ["cli", "api", "api_stream", "desktop"])
def test_query_saves_only_final_answer_and_keeps_previous_copy(
    kb_dir, monkeypatch, narrated_model, entry
):
    from openkb.application.conversations import ask_question

    question = "What is the answer?"
    for _ in range(2):
        if entry == "cli":
            cli = importlib.import_module("openkb.cli")
            monkeypatch.setattr(cli, "_stream_to_tty", lambda: True)
            monkeypatch.setattr(cli, "_setup_llm_key", lambda _: None)
            result = CliRunner().invoke(
                cli.cli, ["--kb-dir", str(kb_dir), "query", question, "--save"]
            )
            assert result.exit_code == 0, result.output
        elif entry.startswith("api"):
            with api_client(kb_dir, monkeypatch) as client:
                result = client.post(
                    "/api/v1/query",
                    json={
                        "kb": "audit",
                        "question": question,
                        "save": True,
                        "stream": entry == "api_stream",
                    },
                )
            assert result.status_code == 200, result.text
        else:
            assert asyncio.run(ask_question(kb_dir, question, save=True)).status == "completed"
    files = list((kb_dir / "wiki/explorations").glob("*.md"))
    assert len(files) == 2
    assert all("The answer is 42." in p.read_text() for p in files)
    assert all("Let me search." not in p.read_text() for p in files)


def test_cli_resume_selects_latest_in_selected_view(kb_dir, monkeypatch):
    from openkb.agent.chat_session import ChatSession

    cli = importlib.import_module("openkb.cli")
    own = ChatSession.new(kb_dir, "model", "en", identity="a-own", view_id="legacy")
    foreign = ChatSession.new(kb_dir, "model", "en", identity="z-foreign", view_id="a" * 32)
    own.save()
    foreign.save()
    selected = []

    async def run_chat(root, session, **kwargs):
        selected.append(session.id)

    monkeypatch.setattr("openkb.agent.chat.run_chat", run_chat)
    monkeypatch.setattr(cli, "_setup_llm_key", lambda _: None)
    result = CliRunner().invoke(
        cli.cli, ["--kb-dir", str(kb_dir), "--view", "legacy", "chat", "--resume"]
    )
    assert result.exit_code == 0, result.output
    assert selected == [own.id]


@pytest.mark.parametrize("entry", ["api", "desktop"])
def test_artifact_export_rejects_external_symlink(kb_dir, tmp_path, monkeypatch, entry):
    from openkb.application.artifacts import export_artifact

    target = kb_dir / "output/skills/example"
    target.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("external-marker")
    try:
        (target / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    if entry == "api":
        with api_client(kb_dir, monkeypatch) as client:
            result = client.get("/api/v1/skill/example/archive", params={"kb": "audit"})
        assert result.status_code == 400
    else:
        destination = tmp_path.parent / f"{tmp_path.name}-export"
        destination.mkdir()
        with pytest.raises(ValueError):
            export_artifact(kb_dir, "output/skills/example", destination)


def test_api_archive_has_same_layout_as_desktop(kb_dir, tmp_path, monkeypatch):
    from openkb.application.artifacts import export_artifact

    target = kb_dir / "output/skills/example"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("# Example")
    destination = tmp_path.parent / f"{tmp_path.name}-export"
    destination.mkdir()
    exported = export_artifact(kb_dir, "output/skills/example", destination)
    with api_client(kb_dir, monkeypatch) as client:
        response = client.get("/api/v1/skill/example/archive", params={"kb": "audit"})
    assert response.status_code == 200
    with zipfile.ZipFile(exported) as desktop, zipfile.ZipFile(io.BytesIO(response.content)) as api:
        assert api.namelist() == desktop.namelist()
        assert [api.read(n) for n in api.namelist()] == [
            desktop.read(n) for n in desktop.namelist()
        ]


@pytest.mark.parametrize("entry", ["cli", "api", "api_stream", "desktop"])
def test_chat_persists_the_same_final_answer(kb_dir, monkeypatch, narrated_model, entry):
    from openkb.agent.chat import _build_style, _run_turn
    from openkb.agent.chat_session import ChatSession, list_sessions, load_session
    from openkb.application.conversations import continue_conversation

    if entry == "cli":
        session = ChatSession.new(kb_dir, "", "")
        asyncio.run(_run_turn(session, "Hi", _build_style(False), use_color=False))
    elif entry.startswith("api"):
        with api_client(kb_dir, monkeypatch) as client:
            result = client.post(
                "/api/v1/chat",
                json={"kb": "audit", "message": "Hi", "stream": entry == "api_stream"},
            )
        assert result.status_code == 200, result.text
    else:
        assert asyncio.run(continue_conversation(kb_dir, "Hi")).status == "completed"
    saved = load_session(kb_dir, list_sessions(kb_dir)[0]["id"])
    assert saved.user_turns == ["Hi"]
    assert saved.assistant_texts[0].startswith("The answer is 42.")
    assert "Let me search." not in saved.assistant_texts[0]
    assert saved.assistant_texts[0] == "The answer is 42."
    assert saved.turn_count == 1
    assert saved.answer_outcomes == ["answered"]


def test_api_session_operations_respect_the_selected_view(kb_dir, monkeypatch):
    from openkb.agent.chat_session import ChatSession

    own = ChatSession.new(kb_dir, "model", "en", view_id="legacy")
    foreign = ChatSession.new(kb_dir, "model", "en", view_id="a" * 32)
    own.save()
    foreign.save()
    with api_client(kb_dir, monkeypatch) as client:
        listed = client.post("/api/v1/chat/sessions", json={"kb": "audit"})
        assert [item["id"] for item in listed.json()["sessions"]] == [own.id]
        for operation in ("load", "delete"):
            result = client.post(
                f"/api/v1/chat/sessions/{operation}",
                json={"kb": "audit", "session_id": foreign.id},
            )
            assert result.status_code == 409, result.text
        assert foreign.path.exists()
        result = client.post(
            "/api/v1/chat/sessions/delete",
            json={"kb": "audit", "session_id": foreign.id, "view_id": foreign.view_id},
        )
        assert result.status_code == 200, result.text
        assert result.json()["deleted"] is True
        assert not foreign.path.exists()


@pytest.mark.parametrize("entry", ["cli", "api", "api_stream", "desktop"])
def test_generation_requires_explicit_replacement_and_preserves_archive(kb_dir, monkeypatch, entry):
    from openkb.application.generators import GenerationOptions, generate_artifact
    from openkb.locks import atomic_write_text
    from openkb.skill.generator import Generator

    (kb_dir / "wiki/concepts/topic.md").write_text("# Topic\nKnowledge")
    target = kb_dir / "output/skills/example"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("original")

    async def generate(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.output_dir / "SKILL.md", "replacement")

    monkeypatch.setattr(Generator, "run", generate)
    for replace in (False, True):
        if entry == "cli":
            cli = importlib.import_module("openkb.cli")
            result = CliRunner().invoke(
                cli.cli,
                ["--kb-dir", str(kb_dir), "skill", "new", "example", "A skill"]
                + (["--yes"] if replace else []),
            )
            assert (result.exit_code == 0) is replace, result.output
        elif entry.startswith("api"):
            with api_client(kb_dir, monkeypatch) as client:
                result = client.post(
                    "/api/v1/skill",
                    json={
                        "kb": "audit",
                        "name": "example",
                        "intent": "A skill",
                        "replace": replace,
                        "stream": entry == "api_stream",
                    },
                )
            if entry == "api_stream":
                assert ("event: final" in result.text) is replace, result.text
                if not replace:
                    assert '"code": 409' in result.text
            else:
                assert result.status_code == (200 if replace else 409), result.text
        else:
            result = asyncio.run(
                generate_artifact(
                    kb_dir,
                    GenerationOptions(
                        "skill", "example", "A skill", overwrite="archive" if replace else "refuse"
                    ),
                )
            )
            assert result.status == ("completed" if replace else "conflict"), result
        assert (target / "SKILL.md").read_text() == ("replacement" if replace else "original")
    copies = [p for p in (target.parent / "example-workspace").rglob("SKILL.md")]
    assert len(copies) == 1
    assert copies[0].read_text() == "original"


@pytest.mark.parametrize("entry", ["query", "chat", "low_level_query"])
@pytest.mark.parametrize(
    "narrated_model",
    [(("<thi", "nk>hidden rationale</think>", "Searching documents..."), "Final answer only.")],
    indirect=True,
)
def test_terminal_shows_final_result_and_filters_reasoning(
    kb_dir, monkeypatch, narrated_model, capsys, entry
):
    if entry == "query":
        cli = importlib.import_module("openkb.cli")
        monkeypatch.setattr(cli, "_stream_to_tty", lambda: True)
        result = CliRunner().invoke(cli.cli, ["--kb-dir", str(kb_dir), "query", "Hi", "--raw"])
        assert result.exit_code == 0, result.output
        output = result.output
    elif entry == "chat":
        from openkb.agent.chat import _build_style, _run_turn
        from openkb.agent.chat_session import ChatSession

        asyncio.run(
            _run_turn(ChatSession.new(kb_dir, "", ""), "Hi", _build_style(False), use_color=False)
        )
        output = capsys.readouterr().out
    else:
        from openkb.agent.query import run_query

        asyncio.run(run_query("Hi", kb_dir, "model", stream=True, raw=True))
        output = capsys.readouterr().out
    assert "Final answer only." in output
    assert "hidden rationale" not in output
    assert "<think>" not in output


@pytest.mark.parametrize("answer", ["x < t", "x < a", "A literal <"])
def test_terminal_final_result_releases_a_held_tag_prefix(answer, capsys):
    from openkb.terminal_answers import AnswerRenderer

    with AnswerRenderer() as renderer:
        for character in answer:
            renderer({"event": "delta", "data": {"text": character}})
        renderer.finish(answer)
    assert capsys.readouterr().out.strip() == answer
