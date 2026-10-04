"""A failed model call cannot keep printing into the next CLI response."""

import json
import time

import pytest

pytest_plugins = ("test_pdf_readback",)


@pytest.mark.parametrize("error_type", [ValueError, SystemExit])
def test_failed_model_call_stops_progress_before_next_cli_response(
    kb_dir, pdf_model, monkeypatch, error_type
):
    from click.testing import CliRunner

    import openkb.documents as documents
    from openkb.agent import compiler
    from openkb.application.documents import import_document
    from openkb.cli import cli

    source = kb_dir / "progress.txt"
    source.write_text("Retained source for a subsequent read.")
    first = import_document(kb_dir, source)
    assert first.status == "added", first.message
    spinners = []
    original = compiler._Spinner

    def spinner(label):
        result = original(label)
        spinners.append(result)
        return result

    def fail(**kwargs):
        raise error_type("Model call failed before its result")

    read = documents.read_document_source

    def delayed_read(*args, **kwargs):
        # The next response stays open past one real progress tick.
        time.sleep(1.1)
        return read(*args, **kwargs)

    monkeypatch.setattr(compiler, "_Spinner", spinner)
    monkeypatch.setattr("litellm.completion", fail)
    monkeypatch.setattr(documents, "read_document_source", delayed_read)
    try:
        with pytest.raises(error_type):
            compiler._llm_call("gpt-4o-mini", [{"role": "user", "content": "Fixture"}], "fixture")
        result = CliRunner().invoke(cli, ["--kb-dir", str(kb_dir), "source", first.source_id])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["source_id"] == first.source_id
        assert all(not item._thread.is_alive() for item in spinners)
    finally:
        for item in spinners:
            if item._thread.is_alive():
                item.stop("Fixture cleanup")
