"""The KB request deadline must reach PageIndex's separate per-index context."""

from ultrakb.indexer import _build_index_config


def test_indexing_honors_kb_timeout_instead_of_short_pageindex_default(monkeypatch, live_llm):
    from pageindex import config
    from pageindex.index.utils import llm_completion

    monkeypatch.setitem(config._LLM_PARAMS, "timeout", 20)
    live_llm.install(monkeypatch)
    before = len(live_llm.calls)
    options = _build_index_config({"timeout": 1200})
    with config.llm_params_scope(options.llm_params):
        assert llm_completion("deepseek/deepseek-flash", "Reply exactly ok.").strip() == "ok"
    assert [call["timeout"] for call in live_llm.calls[before:]] == [1200]
    assert config.get_llm_params()["timeout"] == 20
