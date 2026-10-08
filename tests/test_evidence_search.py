import json

import pytest
from test_answer_evidence_regression import agent_for, invoke


@pytest.mark.asyncio
async def test_original_search_finds_compute_rule_before_external_ntp_rule(kb_dir):
    pages = [
        {"page": 54, "content": "计算节点 NTP 指向管理 VIP，加入集群后自动设置。"},
        {"page": 63, "content": "管理节点的外部 NTP 配置。"},
    ]
    (kb_dir / "wiki/sources/manual.json").write_text(json.dumps(pages))
    result = json.loads(
        await invoke(agent_for(kb_dir), "search_originals", terms=["计算节点", "NTP"])
    )
    assert result["hits"][0]["pages"] == "54"
    assert result["coverage"] == "search_hits_not_exhaustive"
