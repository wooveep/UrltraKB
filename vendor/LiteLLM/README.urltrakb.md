# LiteLLM 本地源码基线

分发名 `litellm`，导入名 `litellm`，本地版本 `1.87.2+urltrakb.2`。固定上游及原文件摘要见 [UPSTREAM.json](UPSTREAM.json)。

本地 `.2` 增加离线初始化和请求隔离。统一执行器、树格式改造和 ConDB 业务接入单独实施。不要把本包已安装解释为 OpenKB 已使用新检索或会话记忆。

保留 MIT 许可证；仅纳入运行包、必要资源及来源/构建说明，排除上游示例数据和独立服务发行。构建与安装方法见根目录 `docs/vendor-sdk.md`。

固定标签与审核过的 sdist 中 `litellm/` 下 2,742 个文件逐字节一致，包含模型表与
其他生成资源。保留该 SDK 目录中的 Proxy 辅助模块，因为 SDK 的可达路径会导入它们；
排除仓库顶层 `enterprise/`、Proxy extras、服务端工作区及独立服务 CLI 入口。
本地 `.1` 未修改 SDK Python 源码；上游导入时初始化行为留给后续任务处理。

本地 `.2`：默认使用带 SHA-256 校验的固定模型表，移除隐式 dotenv 和遥测，延迟导入可选代理 CLI；显式零重试优先、不修改全局重试状态，传入客户端使用请求副本；缺失 usage 不补零或估算。原模型表价格为固定参考，不代表实时价格。维护者只能通过显式 `get_model_cost_map(url, allow_remote=True)` 尝试联网更新。
