import json

import pytest
from test_answer_evidence_regression import agent_for, invoke


@pytest.mark.asyncio
async def test_original_search_finds_compute_rule_before_external_ntp_rule(kb_dir):
    pages = [{"page": n, "unit_kind": "page", "content": "Unrelated page"} for n in range(1, 64)]
    pages[53]["content"] = "计算节点 NTP 指向管理 VIP，加入集群后自动设置。"
    pages[62]["content"] = "管理节点的外部 NTP 配置。"
    (kb_dir / "wiki/sources/manual.json").write_text(json.dumps(pages))
    result = json.loads(
        await invoke(agent_for(kb_dir), "search_originals", terms=["计算节点", "NTP"])
    )
    assert result["hits"][0]["pages"] == "54"
    assert result["coverage"] == "search_hits_not_exhaustive"


@pytest.mark.asyncio
@pytest.mark.parametrize("term", ["x", "Ω", "Настройки", "タイムアウト"])
async def test_original_search_preserves_literal_matching_in_all_scripts(kb_dir, term):
    (kb_dir / "wiki/sources/manual.json").write_text(
        json.dumps(
            [
                {"page": 1, "unit_kind": "page", "content": "Unrelated setup."},
                {"page": 2, "unit_kind": "page", "content": f"{term}: 10"},
            ]
        )
    )
    result = json.loads(await invoke(agent_for(kb_dir), "search_originals", terms=[term]))
    assert result["hits"][0]["pages"] == "2"
