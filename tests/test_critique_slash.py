"""Critique reads accepted artifacts and delivers through the actual SDK boundary."""

import pytest
from artifact_fixtures import publish_artifacts
from error_transport import ErrorTransport
from prompt_toolkit.styles import Style
from stream_transport import StreamTransport
from test_storage_deck_generation import tool_response

from ultrakb.agent.chat import _handle_slash_critique
from ultrakb.application.views import published_scope
from ultrakb.storage.artifact_store import ArtifactStore

_STYLE = Style.from_dict({})
_TARGET = "output/decks/real/index.html"


def accepted_artifact(kb):
    target = kb / _TARGET
    target.parent.mkdir(parents=True)
    target.write_text("<html>Accepted original</html>")
    publish_artifacts(kb, target.parent)
    target.unlink()
    return target


@pytest.mark.asyncio
async def test_critique_no_arg_prints_usage(kb_dir, capsys):
    await _handle_slash_critique("", kb_dir, _STYLE)
    await _handle_slash_critique("   ", kb_dir, _STYLE)
    assert "Usage" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_critique_missing_artifact_prints_error(kb_dir, capsys):
    await _handle_slash_critique("output/decks/missing/index.html", kb_dir, _STYLE)
    assert "[ERROR]" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("absolute", [False, True])
async def test_critique_reads_the_committed_artifact_without_a_projection(
    kb_dir, monkeypatch, capsys, absolute
):
    target = accepted_artifact(kb_dir)
    replacement = "<html>Reviewed retained artifact</html>"
    responses = [
        tool_response("read_output_or_skill_file", {"path": _TARGET}, "read-original"),
        tool_response("write_file", {"path": _TARGET, "content": replacement}, "review"),
        {"content": "Reviewed."},
    ]
    with StreamTransport(responses) as service:
        monkeypatch.setenv("LLM_API_KEY", "offline-critique-key")
        monkeypatch.setenv("OPENAI_API_BASE", service.url)
        await _handle_slash_critique(str(target) if absolute else _TARGET, kb_dir, _STYLE)
        assert len(service.requests) == 3
        assert "Accepted original" in str(service.requests[1])
    scope = published_scope(kb_dir)
    assert ArtifactStore(kb_dir, scope.knowledge.identity).read(_TARGET) == replacement.encode()
    assert "Critique pass complete" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_critique_refuses_an_unaccepted_working_copy(kb_dir, monkeypatch, capsys):
    target = kb_dir / _TARGET
    target.parent.mkdir(parents=True)
    target.write_text("<html>Unaccepted local draft</html>")
    with ErrorTransport(status=400) as service:
        monkeypatch.setenv("OPENAI_API_BASE", service.url)
        await _handle_slash_critique(_TARGET, kb_dir, _STYLE)
        assert service.requests == []
    assert "[ERROR]" in capsys.readouterr().out
    assert target.read_text() == "<html>Unaccepted local draft</html>"


@pytest.mark.asyncio
async def test_critique_reports_an_actual_provider_error(kb_dir, monkeypatch, capsys):
    accepted_artifact(kb_dir)
    with ErrorTransport(status=400) as service:
        monkeypatch.setenv("LLM_API_KEY", "offline-critique-key")
        monkeypatch.setenv("OPENAI_API_BASE", service.url)
        await _handle_slash_critique(_TARGET, kb_dir, _STYLE)
        assert len(service.requests) == 1
    output = capsys.readouterr().out
    assert "[ERROR]" in output and "offline-critique-key" not in output
