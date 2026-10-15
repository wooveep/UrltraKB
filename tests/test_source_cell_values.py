"""Cell reads preserve values and unresolved controls across source consumers."""

import json

import pytest
from test_answer_evidence_regression import agent_for, invoke
from test_source_qualification import freeze_source

from ultrakb.agent.evidence_payload import project_evidence
from ultrakb.agent.source_windows import CompileSourceWindows
from ultrakb.block_package import freeze_block_package, read_block_selection
from ultrakb.text_source import read_text_selection


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["md", "html"])
@pytest.mark.parametrize(
    "body,value,kind",
    [
        ('<input value="42">', None, "unresolved"),
        (
            "<select><option>Disabled</option><option selected>Enabled</option></select>",
            None,
            "unresolved",
        ),
        ("<p>1</p><p>2</p>", "1\n2", "cell"),
        ("<div>1</div><div>2</div>", "1\n2", "cell"),
        ("<pre>1</pre><pre>2</pre>", "1\n2", "cell"),
        ("<span>1</span><span>2</span>", "12", "cell"),
        ("", "", "cell"),
        ("42", "42", "cell"),
        ("1<br>2", "1\n2", "cell"),
    ],
)
async def test_cell_state_and_value_agree_in_block_compile_and_public_read(
    kb_dir, tmp_path, body, value, kind, format
):
    text = f"<table><tr><td>{body}</td><td>Known</td></tr></table>\n"
    source, frozen = freeze_source(kb_dir, tmp_path, format, text)
    package = freeze_block_package(source, "manual")
    raw = json.loads(package.read_text())["source"]
    windows = CompileSourceWindows(kb_dir / "wiki", "manual")
    payloads = [
        project_evidence(read_text_selection(raw)),
        project_evidence(read_block_selection(raw)),
        windows.current_original_evidence(frozen.text),
        windows.read_selection("sources/manual.content.json", {"blocks": "1"})[0],
    ]
    source.unlink()
    response = await invoke(
        agent_for(kb_dir), "get_text_content", doc_name="manual", chars=f"0:{len(frozen.text)}"
    )
    payloads.append(json.loads(response.split("\n\n", 1)[1]))
    for payload in payloads:
        assert payload["content"] == frozen.text
        first, second = payload["native_tables"][0]["cells"]
        assert first["kind"] == kind
        assert first["value_coverage"] == "complete"
        if value is None:
            assert "value" not in first
        else:
            assert first["value"] == value
        assert second["value"] == "Known"
