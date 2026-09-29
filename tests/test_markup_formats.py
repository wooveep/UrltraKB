"""HTML/XML normalize once and retain original structure, resources and policy."""

import pytest

pytest_plugins = ("test_pdf_readback", "test_block_readback")


@pytest.fixture(autouse=True)
def isolated_global_settings(tmp_path, monkeypatch):
    from openkb import config

    directory = tmp_path / "settings"
    monkeypatch.setattr(config, "GLOBAL_CONFIG_DIR", directory)
    monkeypatch.setattr(config, "GLOBAL_CONFIG_PATH", directory / "global.yaml")
    monkeypatch.setattr(config, "GLOBAL_CONFIG_LOCK_PATH", directory / ".lock")


def test_xml_freezes_structure_without_interpreting_business_values(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    original = (
        '<?xml version="1.0"?>\r\n<catalog><entry code="001"><![CDATA[A < B]]>'
        "</entry><!-- kept --></catalog>\r\n"
    )
    path = tmp_path / "catalog.xml"
    path.write_bytes(original.encode("utf-16"))
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    assert result.source_id is not None
    path.unlink()
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["content"] == "```xml\n" + original + "\n```\n"
    assert saved["encoding"]["name"] == "utf-16-le"
    selected = read_document_source(kb_dir, result.source_id, chars="7:28")
    assert selected["content"] == '<?xml version="1.0"?>'
    assert selected["origin_locators"][0]["original_span"] == [1, 22]


@pytest.mark.parametrize(
    "codec,declared,basis",
    [("utf-16-le", "UTF-16LE", "signature"), ("iso8859-1", "ISO-8859-1", "declaration")],
)
def test_xml_honors_encoding_signatures_and_declarations(
    kb_dir, tmp_path, pdf_model, codec, declared, basis
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    text = f'<?xml version="1.0" encoding="{declared}"?><root>café</root>'
    path = tmp_path / "declared.xml"
    path.write_bytes(text.encode(codec))
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["content"] == "```xml\n" + text + "\n```\n"
    assert saved["encoding"]["basis"] == basis


def test_html_keeps_structure_and_freezes_local_and_data_images(kb_dir, tmp_path, pdf_model):
    import base64

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    image = tmp_path / "local.png"
    image.write_bytes(b"local figure")
    embedded = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()
    original = (
        "<!doctype html><html><head><title>Metadata</title><style>p{color:red}</style></head>"
        '<body>Preface<h1 id="intro">Guide</h1><p>First <strong>body</strong> &amp; value.</p>'
        "<ul><li>One</li><li>Two</li></ul><table><tr><th>Name</th><th>Code</th></tr>"
        '<tr><td>Thing</td><td>001</td></tr></table><p><img src="local.png" alt="local">'
        f'<img src="data:image/svg+xml;base64,{embedded}" alt="embedded">'
        '<img src="https://example.test/missing.png" alt="remote"></p>'
        "<script>not_body()</script></body></html>"
    )
    path = tmp_path / "guide.html"
    path.write_bytes(original.encode())
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    assert result.source_id is not None
    path.unlink()
    image.unlink()
    saved = read_document_source(kb_dir, result.source_id)
    assert "# Guide" in saved["content"] and "**body**" in saved["content"]
    assert "* One" in saved["content"] and "| Thing | 001 |" in saved["content"]
    assert "not_body" not in saved["content"] and "p{color" not in saved["content"]
    assert len(saved["assets"]) == 2 and "https://example.test/missing.png" in saved["content"]
    assert saved["resource_policy"] == {"download_remote_assets": False, "source": "default"}
    remote = next(
        resource for resource in saved["resources"] if resource["reference"].startswith("https:")
    )
    assert remote["status"] == "not_requested"
    heading = next(
        origin for origin in saved["origin_locators"] if origin.get("html", {}).get("tag") == "h1"
    )
    a, b = heading["original_span"]
    assert original[a:b].startswith('<h1 id="intro">Guide</h1>')


def test_html_download_policy_freezes_assets_and_single_false_overrides_kb(
    kb_dir, tmp_path, pdf_model, monkeypatch
):
    import io

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    payload = b'<svg xmlns="http://www.w3.org/2000/svg"><text>remote diagram</text></svg>'
    requested = []

    def fetch(request, **kwargs):
        requested.append(request.full_url)
        response = io.BytesIO(payload)
        response.headers = {"Content-Type": "image/svg+xml"}
        return response

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"download_remote_assets": True})
    )
    path = tmp_path / "network.html"
    path.write_text(
        '<h1>hello</h1><p>Body.<img src="https://example.test/chart.svg"></p>', encoding="utf-8"
    )
    imported = import_document(kb_dir, path)
    assert imported.status == "added", imported.message
    saved = read_document_source(kb_dir, imported.source_id)
    assert saved["resource_policy"] == {"download_remote_assets": True, "source": "kb"}
    assert len(requested) == 1 and saved["resources"][0]["status"] == "retained"
    resource = saved["resources"][0]
    assert (
        kb_dir / saved["base_path"] / resource["path"].removeprefix("sources/")
    ).read_bytes() == payload
    again = read_document_source(
        kb_dir, imported.source_id, source_revision_id=imported.source_revision_id
    )
    assert (
        again["normalized_fingerprint"] == saved["normalized_fingerprint"] and len(requested) == 1
    )
    other = tmp_path / "disabled.html"
    other.write_bytes(path.read_bytes())
    disabled = import_document(kb_dir, other, download_remote_assets=False)
    blocked = read_document_source(kb_dir, disabled.source_id)
    assert blocked["resource_policy"] == {"download_remote_assets": False, "source": "single"}
    assert blocked["resources"][0]["status"] == "not_requested" and len(requested) == 1


@pytest.mark.parametrize("adapter", ["cli", "api", "api_stream"])
@pytest.mark.parametrize("protocol_failure", [False, True])
def test_import_override_reports_network_failure_without_losing_html_body(
    kb_dir, tmp_path, pdf_model, monkeypatch, adapter, protocol_failure
):
    import json
    from urllib.error import URLError

    from openkb.documents import read_document_source

    def unavailable(*args, **kwargs):
        if protocol_failure:
            from http.client import IncompleteRead

            raise IncompleteRead(b"partial", 20)
        raise URLError("offline test network")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    path = tmp_path / "offline.html"
    path.write_text('<p>Kept body.<img src="https://example.test/image.png"></p>', encoding="utf-8")
    if adapter == "cli":
        from click.testing import CliRunner

        from openkb.cli import cli
        from openkb.source_catalog import list_sources

        output = CliRunner().invoke(
            cli, ["--kb-dir", str(kb_dir), "add", str(path), "--download-remote-assets"]
        )
        assert output.exit_code == 0, output.output
        source_id = list_sources(kb_dir)[0].source_id
    else:
        from fastapi.testclient import TestClient

        from openkb.api import create_app
        from openkb.config import register_kb_alias

        monkeypatch.delenv("OPENKB_API_TOKEN", raising=False)
        register_kb_alias("markup", kb_dir)
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/v1/add",
                data={
                    "kb": "markup",
                    "stream": str(adapter == "api_stream").lower(),
                    "download_remote_assets": "true",
                },
                files={"files": (path.name, path.read_bytes(), "text/html")},
            )
        assert response.status_code == 200, response.text
        payload = (
            response.json()
            if adapter == "api"
            else next(
                json.loads(item.split("\ndata: ")[1])
                for item in response.text.split("\n\n")
                if item.startswith("event: final\n")
            )
        )
        assert payload["files"][0]["status"] == "added", payload
        source_id = payload["files"][0]["source_id"]
    saved = read_document_source(kb_dir, source_id)
    assert "Kept body." in saved["content"] and "https://example.test/image.png" in saved["content"]
    assert saved["resources"][0]["status"] == "failed"
    assert saved["resource_policy"] == {"download_remote_assets": True, "source": "single"}
    assert any(
        ("IncompleteRead" if protocol_failure else "offline test network") in message
        for message in saved["diagnostics"]
    )


@pytest.mark.parametrize(
    "extension,body,expected",
    [
        ("html", "<p>hello world</p>", "hello world\n\n"),
        ("xml", "<root>hello world</root>", "```xml\n<root>hello world</root>\n```\n"),
    ],
)
def test_markup_uses_frozen_blocks_and_original_anchors(
    kb_dir, tmp_path, block_model, monkeypatch, extension, body, expected
):
    import json

    import litellm

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_kb_config_patch
    from openkb.application.settings_data import KbConfigPatchRequest
    from openkb.documents import read_document_source

    respond = litellm.completion

    def completion(**kwargs):
        result = respond(**kwargs)
        if extension == "xml" and "CONTENT BLOCK STRUCTURE" in str(kwargs["messages"]):
            entries = json.loads(result.choices[0].message.content)
            entries[0]["anchor"]["range"] = [13, 18]
            result.choices[0].message.content = json.dumps(entries)
        return result

    async def acompletion(**kwargs):
        return completion(**kwargs)

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(litellm, "acompletion", acompletion)
    apply_kb_config_patch(
        kb_dir,
        KbConfigPatchRequest(
            kb=str(kb_dir), config={"model": "gpt-4o", "model_capacity": {"max_input_tokens": 1}}
        ),
    )
    path = tmp_path / f"small.{extension}"
    path.write_text(body, encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    path.unlink()
    saved = read_document_source(kb_dir, result.source_id, blocks="1")
    assert saved["content"] == expected
    assert saved["unit_kind"] == "block" and saved["processing"]["length_class"] == "short"
    assert saved["origin_locators"][0]["original_span"][0] == 0


def test_xml_rejects_entities_and_retains_original_for_diagnosis(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    raw = b'<!DOCTYPE root [<!ENTITY external SYSTEM "file:///should-not-read">]><root>&external;</root>'
    path = tmp_path / "entity.xml"
    path.write_bytes(raw)
    result = import_document(kb_dir, path)
    assert result.status == "failed" and "DTDForbidden" in result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["knowledge_revision_id"] is None
    assert (kb_dir / saved["original_path"]).read_bytes() == raw


def test_xml_rejects_declaration_that_disagrees_with_actual_encoding(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    path = tmp_path / "mislabeled.xml"
    path.write_bytes('<?xml version="1.0" encoding="UTF-8"?><root>café</root>'.encode("utf-16"))
    result = import_document(kb_dir, path)
    assert result.status == "failed" and "encoding declaration" in result.message
    source = read_document_source(kb_dir, result.source_id)
    assert source["encoding"]["basis"] == "bom" and source["knowledge_revision_id"] is None


def test_indented_html_images_use_html_rules_and_base_urls(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    path = tmp_path / "base.html"
    path.write_text(
        '<base href="https://example.test/docs/">\n\n'
        '    <p>hello <img src="chart.png">![literal](other.png)</p>\n',
        encoding="utf-8",
    )
    (tmp_path / "chart.png").write_bytes(b"wrong local file")
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert not saved["assets"]
    assert len(saved["resources"]) == 1
    assert saved["resources"][0]["resolved_reference"] == "https://example.test/docs/chart.png"
    assert "https://example.test/docs/chart.png" in saved["content"]
    assert r"!\[literal\](other.png)" in saved["content"]


def test_html_percent_encoded_image_is_available_offline(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    path = tmp_path / "inline.html"
    path.write_text(
        '<p>Diagram <img src="data:image/svg+xml,%3Csvg%20xmlns%3D%22'
        'http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%2F%3E"></p>',
        encoding="utf-8",
    )
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    source = read_document_source(kb_dir, result.source_id)
    image = source["resources"][0]
    assert image["status"] == "retained"
    retained = kb_dir / source["base_path"] / image["path"].removeprefix("sources/")
    assert retained.read_bytes() == b'<svg xmlns="http://www.w3.org/2000/svg"/>'


@pytest.mark.parametrize(
    "kb_value,single,basis,expected",
    [
        (None, None, "global", True),
        (False, None, "kb", False),
        (False, True, "single", True),
    ],
)
def test_html_download_policy_preserves_explicit_false_and_inheritance(
    kb_dir, tmp_path, pdf_model, monkeypatch, kb_value, single, basis, expected
):
    import io

    from openkb.application.documents import import_document
    from openkb.application.settings import apply_global_config_patch, apply_kb_config_patch
    from openkb.application.settings_data import GlobalConfigPatchRequest, KbConfigPatchRequest
    from openkb.documents import read_document_source

    requested = []

    def fetch(request, **kwargs):
        requested.append(request.full_url)
        response = io.BytesIO(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        response.headers = {"Content-Type": "image/svg+xml"}
        return response

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    apply_global_config_patch(GlobalConfigPatchRequest(config={"download_remote_assets": True}))
    apply_kb_config_patch(
        kb_dir, KbConfigPatchRequest(kb=str(kb_dir), config={"download_remote_assets": kb_value})
    )
    path = tmp_path / "precedence.html"
    path.write_text('<p>Diagram <img src="https://example.test/image.svg"></p>', encoding="utf-8")
    result = import_document(kb_dir, path, download_remote_assets=single)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert saved["resource_policy"] == {"source": basis, "download_remote_assets": expected}
    assert len(requested) == int(expected)


def test_watched_html_local_image_change_retains_both_source_revisions(kb_dir, pdf_model):
    from test_native_watch import TaskSink, eventually

    from openkb.application.documents import import_document
    from openkb.documents import read_document_source
    from openkb.runtime.watch import NativeWatch
    from openkb.view_records import SourceMetadata

    path = kb_dir / "raw/diagram.html"
    path.write_text('<p>Diagram <img src="figure.png"></p>', encoding="utf-8")
    figure = kb_dir / "raw/figure.png"
    figure.write_bytes(b"first figure")
    metadata = SourceMetadata(product="Example", applicable_versions=("1",), family="Guide")
    sink = TaskSink()
    watch = NativeWatch(kb_dir, sink, debounce=0.03, scan_interval=0.02)
    try:
        eventually(lambda: len(sink.items) == 1)
        first = import_document(kb_dir, path, metadata=metadata)
        assert first.status == "added", first.message
        sink.finish("0", revision=first.input_version)
        figure.write_bytes(b"second figure")
        eventually(lambda: len(sink.items) == 2)
        second = import_document(kb_dir, path, metadata=metadata)
        assert second.status == "added", second.message
        sink.finish("1", revision=second.input_version)
        before = read_document_source(
            kb_dir, first.source_id, source_revision_id=first.source_revision_id
        )
        after = read_document_source(kb_dir, second.source_id)
        assert (
            first.source_id == second.source_id
            and first.source_revision_id != second.source_revision_id
        )
        assert before["assets"] != after["assets"]
    finally:
        watch.stop()
        assert watch.join(5)


@pytest.mark.parametrize(
    "markup,label,target",
    [
        (
            '<a href="https://example.test/reference"><h1>Linked heading</h1>'
            "<p>Linked body</p></a>",
            "Linked body",
            "https://example.test/reference",
        ),
        (
            '<base href="https://example.test/docs/"><p><a href="reference.html">Reference</a></p>',
            "Reference",
            "https://example.test/docs/reference.html",
        ),
    ],
)
def test_html_preserves_enclosing_and_base_relative_links(
    kb_dir, tmp_path, pdf_model, markup, label, target
):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    path = tmp_path / "links.html"
    path.write_text(markup, encoding="utf-8")
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    saved = read_document_source(kb_dir, result.source_id)
    assert label in saved["content"] and f"]({target})" in saved["content"]
    from markdown_it import MarkdownIt

    rendered = MarkdownIt("commonmark").render(saved["content"])
    assert f'href="{target}"' in rendered
    if "<h1>" in markup:
        assert "<h1>" in rendered


def test_html_heading_and_table_images_remain_readable(kb_dir, tmp_path, pdf_model):
    from openkb.application.documents import import_document
    from openkb.documents import read_document_source

    (tmp_path / "figure.svg").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
    path = tmp_path / "figures.html"
    path.write_text(
        '<h1><img src="figure.svg" alt="architecture"></h1>'
        '<table><tr><td><img src="figure.svg" alt="diagram"></td></tr></table>',
        encoding="utf-8",
    )
    result = import_document(kb_dir, path)
    assert result.status == "added", result.message
    source = read_document_source(kb_dir, result.source_id)
    assert (
        "![architecture](images/" in source["content"] and "![diagram](images/" in source["content"]
    )
    assert all(resource["status"] == "retained" for resource in source["resources"])
