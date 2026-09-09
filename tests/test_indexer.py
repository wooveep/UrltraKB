"""Tests for openkb.indexer."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from openkb.indexer import (
    IndexResult,
    _build_index_config,
    _normalize_page_content,
    index_long_document,
)


class _FakeIndexConfigWithConcurrency:
    """Stand-in for a PageIndex ``IndexConfig`` that declares ``max_concurrency``.

    Used instead of relying on whatever ``pageindex`` happens to be installed in
    this environment, so the forwarding tests are deterministic regardless of
    the currently-pinned PageIndex version (see ``test_forwards_...`` below).
    """

    model_fields = {
        "if_add_node_text": None,
        "if_add_node_summary": None,
        "if_add_doc_description": None,
        "max_concurrency": None,
    }

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeIndexConfigWithoutConcurrency:
    """Stand-in for a PageIndex ``IndexConfig`` predating ``max_concurrency``."""

    model_fields = {
        "if_add_node_text": None,
        "if_add_node_summary": None,
        "if_add_doc_description": None,
    }

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TestBuildIndexConfig:
    def test_sets_base_flags(self):
        cfg = _build_index_config({})
        assert cfg.if_add_node_text is True
        assert cfg.if_add_node_summary is True
        assert cfg.if_add_doc_description is True

    def test_forwards_concurrency_when_supported(self, monkeypatch):
        monkeypatch.setattr("openkb.indexer.IndexConfig", _FakeIndexConfigWithConcurrency)
        cfg = _build_index_config({"concurrency": 8})
        assert cfg.max_concurrency == 8

    def test_does_not_forward_when_unsupported(self, monkeypatch):
        monkeypatch.setattr("openkb.indexer.IndexConfig", _FakeIndexConfigWithoutConcurrency)
        cfg = _build_index_config({"concurrency": 8})
        assert not hasattr(cfg, "max_concurrency")

    def test_none_value_is_left_to_pageindex_default(self, monkeypatch):
        monkeypatch.setattr("openkb.indexer.IndexConfig", _FakeIndexConfigWithConcurrency)
        cfg = _build_index_config({"concurrency": None})
        assert getattr(cfg, "max_concurrency", None) is None

    def test_invalid_value_is_left_to_pageindex_default(self, monkeypatch):
        # resolve_concurrency() rejects bools/non-positive values — same as an
        # unset key, just via the shared config-level validation.
        monkeypatch.setattr("openkb.indexer.IndexConfig", _FakeIndexConfigWithConcurrency)
        cfg = _build_index_config({"concurrency": 0})
        assert getattr(cfg, "max_concurrency", None) is None

    def test_warns_when_configured_but_unsupported(self, monkeypatch, caplog):
        monkeypatch.setattr("openkb.indexer.IndexConfig", _FakeIndexConfigWithoutConcurrency)
        with caplog.at_level(logging.WARNING, logger="openkb.indexer"):
            _build_index_config({"concurrency": 8})
        assert "concurrency" in caplog.text

    def test_no_warning_when_unset(self, monkeypatch, caplog):
        monkeypatch.setattr("openkb.indexer.IndexConfig", _FakeIndexConfigWithoutConcurrency)
        with caplog.at_level(logging.WARNING, logger="openkb.indexer"):
            _build_index_config({})
        assert caplog.text == ""

    def test_no_warning_when_supported(self, monkeypatch, caplog):
        monkeypatch.setattr("openkb.indexer.IndexConfig", _FakeIndexConfigWithConcurrency)
        with caplog.at_level(logging.WARNING, logger="openkb.indexer"):
            _build_index_config({"concurrency": 8})
        assert caplog.text == ""


class TestNormalizePageContent:
    def test_normalizes_pageindex_dicts(self):
        pages = _normalize_page_content(
            [
                {
                    "page_number": "2",
                    "markdown": "  Page two  ",
                    "images": [{"path": "sources/images/doc/a.png"}],
                },
                {"page_num": 3, "text": "Page three", "images": "bad"},
            ]
        )

        assert pages == [
            {
                "page": 2,
                "content": "Page two",
                "images": [{"path": "sources/images/doc/a.png"}],
            },
            {"page": 3, "content": "Page three", "images": []},
        ]

    def test_normalizes_string_pages(self):
        pages = _normalize_page_content([" page one ", "", "page three"])

        assert pages == [
            {"page": 1, "content": "page one", "images": []},
            {"page": 3, "content": "page three", "images": []},
        ]

    def test_rejects_unusable_shapes(self):
        assert _normalize_page_content({"page": 1}) == []
        assert _normalize_page_content([None, {}, {"content": ""}]) == []


class TestIndexLongDocument:
    @pytest.fixture(autouse=True)
    def _local_path_by_default(self, monkeypatch):
        # so they are deterministic regardless of a developer's configured key
        monkeypatch.delenv("PAGEINDEX_API_KEY", raising=False)

    def _make_fake_collection(self, doc_id: str, sample_tree: dict):
        """Build a mock Collection that returns the sample_tree fixture data."""
        col = MagicMock()
        col.add.return_value = doc_id

        # get_document(doc_id, include_text=True) returns full document
        col.get_document.return_value = {
            "doc_id": doc_id,
            "doc_name": sample_tree["doc_name"],
            "doc_description": sample_tree["doc_description"],
            "doc_type": "pdf",
            "structure": sample_tree["structure"],
        }

        # get_page_content returns empty list by default (overridden per test as needed)
        col.get_page_content.return_value = []
        return col

    def _fake_pages(self):
        return [
            {"page": 1, "content": "Page one text.", "images": []},
            {"page": 2, "content": "Page two text.", "images": []},
        ]

    def test_returns_index_result(self, kb_dir, sample_tree, tmp_path):
        doc_id = "abc-123"
        fake_col = self._make_fake_collection(doc_id, sample_tree)

        fake_client = MagicMock()
        fake_client.collection.return_value = fake_col

        pdf_path = tmp_path / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        with (
            patch("openkb.indexer.LocalClient", return_value=fake_client),
            patch("openkb.images.convert_pdf_to_pages", return_value=self._fake_pages()),
        ):
            result = index_long_document(pdf_path, kb_dir)

        assert isinstance(result, IndexResult)
        assert result.doc_id == doc_id
        assert result.description == sample_tree["doc_description"]
        assert result.tree is not None

    def test_deletes_pageindex_doc_when_a_post_add_step_fails(self, kb_dir, sample_tree, tmp_path):
        """The PageIndex blob is durably written by col.add(), but .openkb/files is
        no longer in the add mutation's eager snapshot — track_new only registers
        the blob on a successful return. So if any step after col.add() raises
        (here: get_document), index_long_document must delete the doc it just
        added; otherwise the blob leaks as an orphan that pageindex.db — rolled
        back by the snapshot — no longer references, and no reaper reclaims."""
        doc_id = "abc-123"
        col = self._make_fake_collection(doc_id, sample_tree)
        col.get_document.side_effect = RuntimeError("get_document blew up")

        fake_client = MagicMock()
        fake_client.collection.return_value = col

        pdf_path = tmp_path / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        with patch("openkb.indexer.LocalClient", return_value=fake_client):
            with pytest.raises(RuntimeError, match="get_document blew up"):
                index_long_document(pdf_path, kb_dir)

        col.delete_document.assert_called_once_with(doc_id)

    def test_source_page_written_as_json(self, kb_dir, sample_tree, tmp_path):
        """Long doc source should be written as JSON, not markdown."""
        import json as json_mod

        doc_id = "abc-123"
        fake_col = self._make_fake_collection(doc_id, sample_tree)

        fake_client = MagicMock()
        fake_client.collection.return_value = fake_col
        # Mock get_page_content to return page data
        fake_col.get_page_content.return_value = [
            {"page": 1, "content": "Page one text."},
            {"page": 2, "content": "Page two text."},
        ]

        pdf_path = tmp_path / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        with (
            patch("openkb.indexer.LocalClient", return_value=fake_client),
            patch("openkb.images.convert_pdf_to_pages", return_value=self._fake_pages()),
        ):
            index_long_document(pdf_path, kb_dir)

        json_file = kb_dir / "wiki" / "sources" / "sample.json"
        assert json_file.exists()
        assert not (kb_dir / "wiki" / "sources" / "sample.md").exists()
        data = json_mod.loads(json_file.read_text())
        assert len(data) == 2
        assert data[0]["page"] == 1
        assert data[0]["content"] == "Page one text."

    def test_summary_page_written(self, kb_dir, sample_tree, tmp_path):
        doc_id = "abc-123"
        fake_col = self._make_fake_collection(doc_id, sample_tree)

        fake_client = MagicMock()
        fake_client.collection.return_value = fake_col

        pdf_path = tmp_path / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        with (
            patch("openkb.indexer.LocalClient", return_value=fake_client),
            patch("openkb.images.convert_pdf_to_pages", return_value=self._fake_pages()),
        ):
            index_long_document(pdf_path, kb_dir)

        summary_file = kb_dir / "wiki" / "summaries" / "sample.md"
        assert summary_file.exists()
        content = summary_file.read_text(encoding="utf-8")
        assert "doc_type: pageindex" in content
        assert "Summary:" in content

    def test_localclient_called_with_index_config(self, kb_dir, sample_tree, tmp_path):
        """LocalClient must be created with the correct IndexConfig flags."""
        doc_id = "xyz-456"
        fake_col = self._make_fake_collection(doc_id, sample_tree)

        fake_client = MagicMock()
        fake_client.collection.return_value = fake_col

        pdf_path = tmp_path / "report.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        with (
            patch("openkb.indexer.LocalClient", return_value=fake_client) as mock_cls,
            patch("openkb.images.convert_pdf_to_pages", return_value=self._fake_pages()),
        ):
            index_long_document(pdf_path, kb_dir)

        # Verify PageIndexClient was instantiated with correct IndexConfig
        mock_cls.assert_called_once()
        _, kwargs = mock_cls.call_args
        ic = kwargs.get("index_config")
        assert ic is not None, "index_config must be passed to PageIndexClient"
        assert ic.if_add_node_text is True
        assert ic.if_add_node_summary is True
        assert ic.if_add_doc_description is True

    def test_concurrency_flows_from_kb_config(self, kb_dir, sample_tree, tmp_path):
        """The KB's real config.yaml, loaded by index_long_document itself, must
        reach the IndexConfig passed to PageIndexClient — not just the isolated
        _build_index_config unit tested directly with a hand-built dict."""
        (kb_dir / ".openkb" / "config.yaml").write_text(
            "model: gpt-4o-mini\nconcurrency: 7\n", encoding="utf-8"
        )

        doc_id = "conc-789"
        fake_col = self._make_fake_collection(doc_id, sample_tree)
        fake_client = MagicMock()
        fake_client.collection.return_value = fake_col

        pdf_path = tmp_path / "report.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 fake")

        with (
            patch("openkb.indexer.IndexConfig", _FakeIndexConfigWithConcurrency),
            patch("openkb.indexer.LocalClient", return_value=fake_client) as mock_cls,
            patch("openkb.images.convert_pdf_to_pages", return_value=self._fake_pages()),
        ):
            index_long_document(pdf_path, kb_dir)

        _, kwargs = mock_cls.call_args
        assert kwargs["index_config"].max_concurrency == 7


def test_index_long_document_uses_explicit_doc_name(kb_dir, monkeypatch):
    monkeypatch.delenv("PAGEINDEX_API_KEY", raising=False)

    fake_col = MagicMock()
    fake_col.add.return_value = "doc-123"
    fake_col.get_document.return_value = {
        "doc_name": "original.pdf",
        "doc_description": "desc",
        "structure": [],
    }
    fake_client = MagicMock()
    fake_client.collection.return_value = fake_col

    pdf = kb_dir / "raw" / "original.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    with (
        patch("openkb.indexer.LocalClient", return_value=fake_client),
        patch(
            "openkb.indexer._convert_pdf_to_pages", return_value=[{"page": 1, "text": "p1"}]
        ) as mock_convert,
    ):
        result = index_long_document(pdf, kb_dir, doc_name="original-abc12345")

    assert result.doc_id == "doc-123"
    assert (kb_dir / "wiki" / "sources" / "original-abc12345.json").exists()
    assert (kb_dir / "wiki" / "summaries" / "original-abc12345.md").exists()
    # nothing written under the raw stem
    assert not (kb_dir / "wiki" / "sources" / "original.json").exists()
    assert not (kb_dir / "wiki" / "summaries" / "original.md").exists()
    # the page extractor receives the explicit doc_name and its images dir
    expected_images = kb_dir / "wiki" / "sources" / "images" / "original-abc12345"
    mock_convert.assert_called_once_with(pdf, "original-abc12345", expected_images)
    # summary frontmatter points full_text at the doc_name artifact
    summary_text = (kb_dir / "wiki" / "summaries" / "original-abc12345.md").read_text(
        encoding="utf-8"
    )
    assert "original-abc12345" in summary_text


def test_write_long_doc_artifacts_writes_json_and_summary(kb_dir, sample_tree):
    from openkb.indexer import _write_long_doc_artifacts

    pages = [{"page": 1, "content": "Hello.", "images": []}]
    summary_path = _write_long_doc_artifacts(sample_tree, pages, "my-doc", "doc-1", kb_dir)

    assert summary_path == kb_dir / "wiki" / "summaries" / "my-doc.md"
    assert summary_path.exists()
    json_file = kb_dir / "wiki" / "sources" / "my-doc.json"
    assert json_file.exists()
    assert '"content": "Hello."' in json_file.read_text(encoding="utf-8")
    assert "doc_type: pageindex" in summary_path.read_text(encoding="utf-8")
