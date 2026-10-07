# ChatIndex 本地源码基线

分发名 `ictree`，导入名 `ctree`，本地版本 `0.1.0+urltrakb.3`。固定上游及原文件摘要见 [UPSTREAM.json](UPSTREAM.json)。

本票只纳管源码、修正包身份和依赖，并验证现有调用行为。模型运行时注入、无副作用初始化、树格式改造和 ConDB 业务接入由后续任务完成。不要把本包已安装解释为 OpenKB 已使用新检索或会话记忆。

保留 Apache-2.0 许可证；仅纳入运行包、必要资源及来源/构建说明，排除上游示例数据和独立服务发行。构建与安装方法见根目录 `docs/vendor-sdk.md`。

本地 `0.1.0+urltrakb.2` 拆分 nodes/builder/prompts/state/export/llm。
创建 `CTree(llm=client, analysis_roles=("user",), conversation_id=...)`，
使用 `add_exchange(turn_id, user, assistant)` 幂等追加完整轮次。
命名、分类、连续拆分、最近窗口、冻结摘要和一次格式修复都只分析允许角色。
网络/鉴权/预算/取消错误原样传播，候选状态整轮成功后才发布。

`snapshot_state()` 返回 `ctree.snapshot` v1，`restore_state(..., llm=...)`
恢复完整原文、稳定节点 ID、显式顺序、当前节点、冻结分支与计数器。
`export_retrieval_tree()` 返回独立 `ctree.retrieval` v1，只带允许角色的完整文本；
范围为轮次 [start_turn,end_turn)，不再生成 200 字预览。前缀摘要使用 canonical
JSON（UTF-8、sort_keys、无空格）的 SHA256：轮次稳定 ID、ordinal 和各角色原始
正文摘要构成完整有序列表。检索导出保留各角色摘要以验证来源前缀，隐藏不允许的正文。
旧助手参与的主题拒绝转换成 user-only；旧无版本/预览检查点必须重建。

`refresh_summaries()` 明确执行模型调用。snapshot/restore/export 和 save 均不调用
模型；前三者不写文件，save 仅执行调用方明确要求的保存。隐式 auto_save_path 被拒绝。
兼容 `add(messages, turn_id=...)` 要求明确唯一 user/assistant 和可选 system。
独立 provider 使用 `ctree.llm.create_client(model=..., api_key=..., base_url=...)`；
不读取 .env，不含 OpenAI SDK 直连或第二层重试，凭据/客户端不会进入检查点。

本地 `.3` 仅更新 LiteLLM 精确依赖到 `1.87.2+urltrakb.3`；算法、检查点及检索交换格式不变。
