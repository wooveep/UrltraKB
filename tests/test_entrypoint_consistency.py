"""Business outcomes agree across adapters, using the desktop use cases as reference."""

import asyncio
import importlib
import io
import zipfile

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

pytest_plugins = ("test_vendor_sdk",)


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    monkeypatch.setattr("ultrakb.config.GLOBAL_CONFIG_DIR", profile)
    monkeypatch.setattr("ultrakb.config.GLOBAL_CONFIG_PATH", profile / "global.yaml")
    monkeypatch.delenv("ULTRAKB_API_TOKEN", raising=False)


def api_client(kb_dir, monkeypatch):
    from ultrakb.api import create_app

    monkeypatch.setattr("ultrakb.api_helpers.resolve_kb_alias", lambda _: kb_dir)
    return TestClient(create_app())


@pytest.mark.parametrize("entry", ["cli", "api", "api_stream", "desktop"])
def test_question_wording_never_requires_product_alias_confirmation(
    kb_dir, monkeypatch, narrated_model, entry
):
    from ultrakb.application.conversations import ask_question

    question = "What does Atlas V99 say about the answer?"
    if entry == "cli":
        cli = importlib.import_module("ultrakb.cli")
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
        assert result.answer_outcome == "answered"
        body = result.answer
    assert "42" in body
    assert any("42" in str(run.final_output) for run in narrated_model)


@pytest.mark.parametrize("entry", ["cli", "api", "desktop"])
def test_import_batch_keeps_initial_model(kb_dir, monkeypatch, narrated_model, live_llm, entry):
    from ultrakb.application.documents import import_document
    from ultrakb.application.execution import ExecutionContext
    from ultrakb.locks import atomic_write_text

    settings = kb_dir / ".ultrakb/config.yaml"
    incoming = kb_dir / "incoming"
    incoming.mkdir()
    paths = [incoming / "alpha.md", incoming / "beta.md"]
    for path in paths:
        path.write_text(f"# {path.stem}\nThe {path.stem} approval is recorded before release.")
    models = []

    def observe(values, delivered):
        models.append(values["model"])
        if len(models) == 2:
            atomic_write_text(settings, "model: deepseek/changed-after-capture\nlanguage: zh\n")
        return delivered

    def completion(**kwargs):
        return observe(kwargs, live_llm.completion(**kwargs))

    async def acompletion(**kwargs):
        return observe(kwargs, await live_llm.acompletion(**kwargs))

    monkeypatch.setattr("litellm.completion", completion)
    monkeypatch.setattr("litellm.acompletion", acompletion)
    if entry == "cli":
        cli = importlib.import_module("ultrakb.cli")
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
    assert len(models) >= 4
    assert set(models) == {"deepseek/deepseek-flash"}


@pytest.fixture
def narrated_model(kb_dir, monkeypatch, live_llm):
    """Observe actual final outputs through the project's genuine SDK adapters."""
    from agents import Runner

    from ultrakb.locks import atomic_write_text

    live_llm.install(monkeypatch)
    atomic_write_text(
        kb_dir / ".ultrakb/config.yaml",
        "model: deepseek/deepseek-flash\nlanguage: en\nmodel_capacity:\n"
        "  max_input_tokens: 65536\n  tokenizer_model: gpt-4o-mini\nrole_generation:\n"
        "  answer:\n    max_tokens: 256\n    reasoning_effort: none\n"
        "  compile:\n    max_tokens: 512\n    reasoning_effort: none\n",
    )
    atomic_write_text(
        kb_dir / "wiki/index.md", "# Atlas V99\nRead sources/fixture.md for the recorded answer."
    )
    atomic_write_text(kb_dir / "wiki/sources/fixture.md", "# Atlas V99\nThe answer is 42.")
    actual = []
    run_streamed, run = Runner.run_streamed, Runner.run

    def observe_stream(*args, **kwargs):
        delivered = run_streamed(*args, **kwargs)
        actual.append(delivered)
        return delivered

    async def observe(*args, **kwargs):
        delivered = await run(*args, **kwargs)
        actual.append(delivered)
        return delivered

    monkeypatch.setattr(Runner, "run_streamed", observe_stream)
    monkeypatch.setattr(Runner, "run", observe)
    return actual


@pytest.mark.parametrize("entry", ["cli", "api", "api_stream", "desktop"])
def test_query_saves_only_final_answer_and_keeps_previous_copy(
    kb_dir, monkeypatch, narrated_model, entry
):
    from ultrakb.application.conversations import ask_question

    question = "What is the answer?"
    for _ in range(2):
        if entry == "cli":
            cli = importlib.import_module("ultrakb.cli")
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
    answers = [str(run.final_output) for run in narrated_model if run.final_output]
    assert len(answers) >= 2
    assert all("42" in p.read_text() for p in files)
    assert all(any(answer in p.read_text() for answer in answers) for p in files)
    assert all("Let me search." not in p.read_text() for p in files)


def test_cli_resume_selects_latest_shared_session(kb_dir, monkeypatch):
    from ultrakb.agent.chat_session import ChatSession

    cli = importlib.import_module("ultrakb.cli")
    previous = ChatSession.new(kb_dir, "model", "en", identity="a-previous")
    current = ChatSession.new(kb_dir, "model", "en", identity="z-current")
    previous.save()
    current.save()
    selected = []

    async def run_chat(root, session, **kwargs):
        selected.append(session.id)

    monkeypatch.setattr("ultrakb.agent.chat.run_chat", run_chat)
    monkeypatch.setattr(cli, "_setup_llm_key", lambda _: None)
    result = CliRunner().invoke(cli.cli, ["--kb-dir", str(kb_dir), "chat", "--resume"])
    assert result.exit_code == 0, result.output
    assert selected == [current.id]


@pytest.mark.parametrize("entry", ["api", "desktop"])
def test_artifact_export_rejects_external_symlink(kb_dir, tmp_path, monkeypatch, entry):
    from ultrakb.application.artifacts import export_artifact

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
    from ultrakb.application.artifacts import export_artifact

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
    from ultrakb.agent.chat import _build_style, _run_turn
    from ultrakb.agent.chat_session import ChatSession, list_sessions, load_session
    from ultrakb.application.conversations import continue_conversation

    prompt = "Read sources/fixture.md and state the recorded answer."
    if entry == "cli":
        session = ChatSession.new(kb_dir, "", "")
        asyncio.run(_run_turn(session, prompt, _build_style(False), use_color=False))
    elif entry.startswith("api"):
        with api_client(kb_dir, monkeypatch) as client:
            result = client.post(
                "/api/v1/chat",
                json={"kb": "audit", "message": prompt, "stream": entry == "api_stream"},
            )
        assert result.status_code == 200, result.text
    else:
        assert asyncio.run(continue_conversation(kb_dir, prompt)).status == "completed"
    saved = load_session(kb_dir, list_sessions(kb_dir)[0]["id"])
    assert saved.user_turns == [prompt]
    assert "42" in saved.assistant_texts[0]
    assert "Let me search." not in saved.assistant_texts[0]
    assert saved.assistant_texts[0] == str(narrated_model[-1].final_output)
    assert saved.turn_count == 1
    assert saved.answer_outcomes == ["answered"]


def test_api_session_operations_use_shared_sessions(kb_dir, monkeypatch):
    from ultrakb.agent.chat_session import ChatSession

    own = ChatSession.new(kb_dir, "model", "en")
    own.save()
    with api_client(kb_dir, monkeypatch) as client:
        listed = client.post("/api/v1/chat/sessions", json={"kb": "audit"})
        assert [item["id"] for item in listed.json()["sessions"]] == [own.id]
        loaded = client.post(
            "/api/v1/chat/sessions/load", json={"kb": "audit", "session_id": own.id}
        )
        assert loaded.status_code == 200, loaded.text
        invalid = client.post(
            "/api/v1/chat/sessions/load",
            json={"kb": "audit", "session_id": own.id, "view_id": "a" * 32},
        )
        assert invalid.status_code == 422
        assert own.path.exists()
        result = client.post(
            "/api/v1/chat/sessions/delete", json={"kb": "audit", "session_id": own.id}
        )
        assert result.status_code == 200, result.text
        assert result.json()["deleted"] is True
        assert not own.path.exists()


@pytest.mark.parametrize("entry", ["cli", "api", "api_stream", "desktop"])
def test_generation_requires_explicit_replacement_and_preserves_archive(
    kb_dir, monkeypatch, narrated_model, model_service, entry
):
    from short_generation import assert_formatter_delivery, retained_formatter

    from ultrakb.application.generators import GenerationOptions, generate_artifact

    intent = (
        "Required deliverable: SKILL.md must include a 'Recorded answer:' line containing "
        "the numeric value read from the original source. Read that value from the source; "
        "do not guess it from this user prompt. "
    ) + retained_formatter(kb_dir, model_service.credentials, recorded_answer=42)
    target = kb_dir / "output/skills/example"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("original")

    for replace in (False, True):
        if entry == "cli":
            cli = importlib.import_module("ultrakb.cli")
            result = CliRunner().invoke(
                cli.cli,
                [
                    "--kb-dir",
                    str(kb_dir),
                    "skill",
                    "new",
                    "example",
                    intent,
                ]
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
                        "intent": intent,
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
                        "skill",
                        "example",
                        intent,
                        overwrite="archive" if replace else "refuse",
                    ),
                )
            )
            assert result.status == ("completed" if replace else "conflict"), result
        content = (target / "SKILL.md").read_text()
        if replace:
            assert content != "original" and "42" in content
            assert_formatter_delivery(kb_dir, "example", model_service)
            assert narrated_model
        else:
            assert content == "original"
            assert model_service.requests == [] and narrated_model == []
    copies = [p for p in (target.parent / "example-workspace").rglob("SKILL.md")]
    assert len(copies) == 1
    assert copies[0].read_text() == "original"
    assert copies[0].read_text() == "original"


@pytest.mark.parametrize("entry", ["query", "chat", "low_level_query"])
def test_terminal_shows_final_result_and_filters_reasoning(
    kb_dir, monkeypatch, narrated_model, capsys, entry
):
    prompt = "Read sources/fixture.md and state the recorded answer."
    if entry == "query":
        cli = importlib.import_module("ultrakb.cli")
        monkeypatch.setattr(cli, "_stream_to_tty", lambda: True)
        result = CliRunner().invoke(cli.cli, ["--kb-dir", str(kb_dir), "query", prompt, "--raw"])
        assert result.exit_code == 0, result.output
        output = result.output
    elif entry == "chat":
        from ultrakb.agent.chat import _build_style, _run_turn
        from ultrakb.agent.chat_session import ChatSession

        asyncio.run(
            _run_turn(ChatSession.new(kb_dir, "", ""), prompt, _build_style(False), use_color=False)
        )
        output = capsys.readouterr().out
    else:
        from ultrakb.agent.query import run_query

        asyncio.run(run_query(prompt, kb_dir, "model", stream=True, raw=True))
        output = capsys.readouterr().out
    assert "42" in output
    assert "hidden rationale" not in output
    assert "<think>" not in output


@pytest.mark.parametrize("answer", ["x < t", "x < a", "A literal <"])
def test_terminal_final_result_releases_a_held_tag_prefix(answer, capsys):
    from ultrakb.terminal_answers import AnswerRenderer

    with AnswerRenderer() as renderer:
        for character in answer:
            renderer({"event": "delta", "data": {"text": character}})
        renderer.finish(answer)
    assert capsys.readouterr().out.strip() == answer
