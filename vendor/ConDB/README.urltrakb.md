# ConDB 本地源码基线

分发名 `pageindex-condb`，导入名 `contextdb`，本地版本 `1.0+urltrakb.4`。固定上游及原文件摘要见 [UPSTREAM.json](UPSTREAM.json)。

本地补丁加入无损文档转换、共享原生 TreeDB、保存点事务、稳定顺序和封存只读。文档及节点的原始对象完整保留，定位字段维持各自单位，不猜测为 PDF 页。存储/转换不调用模型。查询运行时及 OpenKB 检索/会话接入另行实施。

保留 Apache-2.0 许可证；仅纳入运行包、必要资源及来源/构建说明，排除上游示例数据和独立服务发行。构建与安装方法见根目录 `docs/vendor-sdk.md`。

最低兼容补丁：转换 assistant 内容块时按原顺序连接全部文本，避免只保留最后一段。
`chat` / `chat_with_cache` 通过真实 LiteLLM 1.87.2 HTTP 往返验证工具、缓存、异常及 usage；
依赖从上游约束调整为项目固定版本，未通过降低 OpenAI 或 Agents SDK 版本绕过冲突。

### 存储契约（ConDB schema 1）

`ConDB(storage=db)` 与 `ContextTree(storage=db)` 共用原生 TreeDB，由调用者负责关闭。
`with db.transaction():` 可组合树与业务表写入；嵌套使用 SQLite SAVEPOINT，绝不提前提交外层事务。
`db.checkpoint()` 在无活动事务时截断 WAL 并执行完整性检查；随后 `db.close()`，再复制主文件。
`TreeDB(path, read_only=True)` 只打开已封存 schema 1，使用 immutable/ro URI，不创建表、WAL 或迁移。
缺失文件、未知/旧 schema、未 checkpoint 的 WAL 显式失败。新节点路径按显式插入顺序编码，source node_id 保留。
文档需有 doc_name、有序 structure、唯一 node_id、title 和显式定位；未知 JSON 扩展随完整 document/source 对象保留。

### 会话交换契约（ctree.retrieval v1）

`ChatIndexAdapter` 仅接受完整版本化检索导出。验证角色策略、所有允许角色的原文摘要、源前缀摘要、稳定身份、明确轮次范围与完整覆盖；正文不截断。
前缀摘要使用每轮所有源角色的 content_digests，导出只携带允许角色的正文；转换重新验证这些正文的摘要，不需要或恢复排除角色的正文。
主题的 analysis_roles 必须属于 content_roles，不能将含助手分析的主题标记为 user-only。
旧 topics/subtopics、tree/conversation 和预览均拒绝，不猜测消息配对，也不调用模型迁移。
旧 `contextdb.adapter.base.ChatIndexAdapter` import 保持可用，正式模块为 `contextdb.adapter.chatindex`。

本地 `.4` 仅更新 LiteLLM 精确依赖到 `1.87.2+urltrakb.3`；数据库 schema 与交换格式不变。
