# ConDB 本地源码基线

分发名 `pageindex-condb`，导入名 `contextdb`，本地版本 `1.0+urltrakb.2`。固定上游及原文件摘要见 [UPSTREAM.json](UPSTREAM.json)。

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
