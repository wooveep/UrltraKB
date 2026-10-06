# ConDB 本地源码基线

分发名 `pageindex-condb`，导入名 `contextdb`，本地版本 `1.0+urltrakb.1`。固定上游及原文件摘要见 [UPSTREAM.json](UPSTREAM.json)。

本票只纳管源码、修正包身份和依赖，并验证现有调用行为。模型运行时注入、无副作用初始化、树格式改造和 ConDB 业务接入由后续任务完成。不要把本包已安装解释为 OpenKB 已使用新检索或会话记忆。

保留 Apache-2.0 许可证；仅纳入运行包、必要资源及来源/构建说明，排除上游示例数据和独立服务发行。构建与安装方法见根目录 `docs/vendor-sdk.md`。

最低兼容补丁：转换 assistant 内容块时按原顺序连接全部文本，避免只保留最后一段。
`chat` / `chat_with_cache` 通过真实 LiteLLM 1.87.2 HTTP 往返验证工具、缓存、异常及 usage；
依赖从上游约束调整为项目固定版本，未通过降低 OpenAI 或 Agents SDK 版本绕过冲突。
