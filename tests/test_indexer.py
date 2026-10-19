"""Tests for ultrakb.indexer."""

from __future__ import annotations

import logging

from ultrakb.indexer import _build_index_config


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

    def model_dump(self, **kwargs):
        return {k: v for k, v in self.__dict__.items() if k in self.model_fields}


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
        monkeypatch.setattr("ultrakb.indexer.IndexConfig", _FakeIndexConfigWithConcurrency)
        cfg = _build_index_config({"concurrency": 8})
        assert cfg.max_concurrency == 8

    def test_does_not_forward_when_unsupported(self, monkeypatch):
        monkeypatch.setattr("ultrakb.indexer.IndexConfig", _FakeIndexConfigWithoutConcurrency)
        cfg = _build_index_config({"concurrency": 8})
        assert not hasattr(cfg, "max_concurrency")

    def test_none_value_is_left_to_pageindex_default(self, monkeypatch):
        monkeypatch.setattr("ultrakb.indexer.IndexConfig", _FakeIndexConfigWithConcurrency)
        cfg = _build_index_config({"concurrency": None})
        assert getattr(cfg, "max_concurrency", None) is None

    def test_invalid_value_is_left_to_pageindex_default(self, monkeypatch):
        # resolve_concurrency() rejects bools/non-positive values — same as an
        # unset key, just via the shared config-level validation.
        monkeypatch.setattr("ultrakb.indexer.IndexConfig", _FakeIndexConfigWithConcurrency)
        cfg = _build_index_config({"concurrency": 0})
        assert getattr(cfg, "max_concurrency", None) is None

    def test_warns_when_configured_but_unsupported(self, monkeypatch, caplog):
        monkeypatch.setattr("ultrakb.indexer.IndexConfig", _FakeIndexConfigWithoutConcurrency)
        with caplog.at_level(logging.WARNING, logger="ultrakb.indexer"):
            _build_index_config({"concurrency": 8})
        assert "concurrency" in caplog.text

    def test_no_warning_when_unset(self, monkeypatch, caplog):
        monkeypatch.setattr("ultrakb.indexer.IndexConfig", _FakeIndexConfigWithoutConcurrency)
        with caplog.at_level(logging.WARNING, logger="ultrakb.indexer"):
            _build_index_config({})
        assert caplog.text == ""

    def test_no_warning_when_supported(self, monkeypatch, caplog):
        monkeypatch.setattr("ultrakb.indexer.IndexConfig", _FakeIndexConfigWithConcurrency)
        with caplog.at_level(logging.WARNING, logger="ultrakb.indexer"):
            _build_index_config({"concurrency": 8})
        assert caplog.text == ""
