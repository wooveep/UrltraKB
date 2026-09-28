"""The KB request deadline must reach PageIndex's separate per-index context."""

from types import SimpleNamespace

from openkb.indexer import _build_index_config


def test_indexing_honors_kb_timeout_instead_of_short_pageindex_default(monkeypatch):
    from pageindex import config
    from pageindex.index.utils import llm_completion

    monkeypatch.setitem(config._LLM_PARAMS, "timeout", 20)
    seen = []

    def completion(**kwargs):
        seen.append(kwargs["timeout"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr("litellm.completion", completion)
    options = _build_index_config({"timeout": 1200})
    with config.llm_params_scope(options.llm_params):
        assert llm_completion("deepseek/deepseek-flash", "slow reasoning request") == "ok"
    assert seen == [1200]
    assert config.get_llm_params()["timeout"] == 20
