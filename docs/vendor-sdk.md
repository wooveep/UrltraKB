# 本地 SDK 来源与发行基线

四个 SDK 是仓库内普通源码依赖，开发、CLI、桌面和 API 共享同一份本地
`litellm`。本阶段维持现有编译与问答行为；ChatIndex 和 ConDB 的运行时注入、
知识数据库和会话记忆接入由后续任务交付。

| 目录 | 分发名 / 导入名 | 本地版本 | 上游提交 | 许可证 |
| --- | --- | --- | --- | --- |
| PageIndex | pageindex / pageindex | 0.3.0.dev3+urltrakb.5 | 9ad54122bbd519cec8913198e2d63cff92781c1e | MIT |
| ChatIndex | ictree / ctree | 0.1.0+urltrakb.1 | 7df2c9208db6f113f85a6c09295bec7f0f2114e7 | Apache-2.0 |
| ConDB | pageindex-condb / contextdb | 1.0+urltrakb.1 | 62da030426b3eee96a77b464e7007cdf8530c42e | Apache-2.0 |
| LiteLLM | litellm / litellm | 1.87.2+urltrakb.2 | 1296275dc52d9f4e05696380735037fbb841fcc3 | MIT |

每个目录的 `UPSTREAM.json` 记录原文件 SHA-256、选定与排除范围、实际纳入文件、
新增/修改/删除的本地补丁。包内 `_urltrakb_source.json` 同时进入 editable 和 wheel，
用于识别分发版本和来源提交。清单自身不参与自我摘要；根源码导出清单覆盖它。

LiteLLM v1.87.2 的审核 sdist SHA-256 为
`36b172cf8824bed6ae17fb8031a021c8bb2046014303c2220045868f1230690a`。
该 sdist 与固定标签的 `litellm/` 内 2,742 个文件逐字节一致，模型表等资源无差异。
完整保留 SDK 子树及其可达 Proxy 辅助模块；排除顶层 enterprise、Proxy extras、
服务工作区和独立服务 CLI。ChatGPT 与 GitHub Copilot provider 源码保持原样。

PageIndex 的 `.5` 代码保留不变，只校正此前仍标 `.4` 的来源说明并固定依赖。
ConDB 的最小兼容补丁按原顺序保留全部 assistant 文本块，不再只保留最后一段。
ChatIndex 的占位作者/项目元数据和 MIT 分类已修正。新增 SDK 源码补丁须提升相应
本地版本；打包版本不等于内容策略版本，不能单凭版本变化要求付费重建旧索引。

## 安装

Python 范围统一为 `>=3.10,<3.14`。OpenAI `2.44.0`、Agents SDK `0.17.3`、
tiktoken `0.13.0`、Jinja2 `3.1.6`、PyYAML `6.0.3` 和 dotenv `1.2.2` 保持固定。
前三包使用 `poetry-core==2.4.1`，LiteLLM 使用 `uv_build==0.11.8`。

```sh
uv sync --frozen --extra dev --extra desktop --extra api
uv run python scripts/local_vendors.py
```

pip 安装需同时提供四个本地包，不依赖安装顺序或 `--no-deps`：

```sh
pip install -e ./vendor/PageIndex -e ./vendor/ChatIndex -e ./vendor/ConDB -e ./vendor/LiteLLM -e ".[desktop,api,dev]"
```

## 构建与验证

```sh
uv build vendor/LiteLLM --out-dir dist
uv build vendor/PageIndex --out-dir dist
uv build vendor/ChatIndex --out-dir dist
uv build vendor/ConDB --out-dir dist
uv build --out-dir dist
uv run python scripts/verify_vendor_artifacts.py --dist dist
uv export --frozen --format requirements-txt --no-dev --no-emit-project --no-emit-package pageindex --no-emit-package ictree --no-emit-package pageindex-condb --no-emit-package litellm --no-hashes --output-file /tmp/openkb-wheel-constraints.txt
uv venv /tmp/openkb-wheel-check
uv pip install --python /tmp/openkb-wheel-check/bin/python --constraint /tmp/openkb-wheel-constraints.txt --find-links dist dist/openkb-*.whl
uv pip check --python /tmp/openkb-wheel-check/bin/python
LITELLM_LOCAL_MODEL_COST_MAP=True /tmp/openkb-wheel-check/bin/python scripts/verify_vendor_install.py --wheel
```

根 wheel 依赖四个独立 vendor wheel；根 sdist 和提交源码导出包含全部 vendor 源码、
许可证、模板、配置、模型表和构建清单。开发/冻结构建校验当前 checkout 的导入路径及
源码摘要；安装包校验实际分发版本、来源身份与资源，不把 site-packages 错判为源码树。

| 验证边界 | 已执行证据 |
| --- | --- |
| 当前短文编译 | 本地记录 HTTP 服务，实际编译入口写出摘要 |
| 普通问答 | 实际 run_query 入口返回模型回答并保留证据范围说明 |
| Agent 工具与流式回答 | 真实 Agents SDK、LiteLLM、read_file 往返、流式事件与 usage |
| ConDB 兼容 | 实际 tools、缓存块、文本、认证异常及 OpenAI/Anthropic usage |
| 来源清单 | 修改、删除、添加及嵌套 Git 元数据均被拒绝 |
| wheel / sdist | 五包构建成功；四 SDK 运行文件、原许可证及 2,914 个 vendor 源文件逐字节核对 |
| 干净安装 | Linux / CPython 3.12.13，按锁文件约束安装 123 个包（含测试工具），依赖检查通过；脱离 checkout 重跑 7 项 SDK 测试通过 |
| wheel 资源与 provider | 实际读取 ConDB YAML、加载 Jinja 模板及 LiteLLM 模型表；ChatGPT/Copilot chat 和 Responses 模块可导入，不执行订阅登录 |
| PyInstaller 收集 | 固定 6.22.2 / hooks 2026.7；四包资源与递归元数据可收集，ctree/contextdb 的 4/33 个模块可加载 |
| 仓库回归 | 全量 pytest：1994 通过、44 跳过；ruff 通过；mypy 检查 310 个源码文件通过；Standards / Spec 审查均无发现 |

2026-10-06 执行上述验证。回环测试可复制 `tests/test_vendor_sdk.py` 和 `tests/conftest.py`
到仓库外，在装有固定 pytest/pytest-asyncio 的 wheel 环境运行，避免源码目录遮蔽已安装包。
仓库内验证命令：

```sh
uv run pytest tests/test_vendor_sdk.py tests/test_vendor_packages.py tests/test_local_pageindex.py tests/test_desktop_source.py
```

原始第三方源码通过摘要核对，
不统一格式化；新增 OpenKB 脚本和测试遵守根 lint，vendor 的行为补丁通过实际接口测试。
OpenKB 原有 800 行约束不变。LiteLLM `.2` 已实现离线初始化、请求隔离和缺失 usage 保真；统一执行政策由 OpenKB 执行器负责。
全量运行出现 9 条上游警告（异步日志清理及 Pydantic 弃用），保留为后续运行时整合基线。
这里只记录实际 SDK/源码/发行验证，不代表 Windows 或 Debian 完整桌面发行已验收。

## LiteLLM 离线与冻结验证

本地模型表附带 `_urltrakb_model_table.json`，加载时核对 SHA-256。未知模型不推测上下文容量，固定表价格不标为实时价格。默认无网络导入、dotenv、遥测或预置回调。请求显式传入凭据、端点、超时及 `num_retries=0,max_retries=0`；上层持有重试政策。

开发和 wheel 环境可运行 `tests/test_litellm_policy.py` 与 `tests/test_vendor_sdk.py`；前者在新进程禁止 socket 联网并放置诱饵 `.env`，后者通过实际回环 HTTP 验证 SDK 及 Agents。冻结发行必须包含四个包的数据、元数据、模型表及身份摘要；执行 `scripts/verify_vendor_install.py` 核对资源。Windows/macOS 订阅登录和实际冻结发行需对应平台人工验证。

LiteLLM `.2` 验证（2026-10-06）：开发与独立 wheel 环境分别通过 28 项真实 SDK/传输测试（含无网导入、部分回执、取消与关闭、双端点、共享客户端请求绑定），独立环境 127 个包依赖兼容；四 SDK wheel/sdist 及根源码包通过来源、许可和文件摘要核对。mypy 与 ruff 通过，Standards / Spec 复核无遗留发现。

## 任务模型执行政策

编译通过 `openkb/llm_execution.py`，在 KB 租约取得后由 `ExecutionContext` 冻结角色模型、凭据和政策。默认所有角色继承 `model`；可选 `conversation_model`、`retrieval_model` 只覆盖模型名，共享同一凭据与端点。运行中编辑配置只影响新任务。

KB 的 `model_policy` 可设置 `max_calls`（默认 1000）、`concurrency`（5）、`retries`（2）、`backoff`（0.25 秒）及可选 `deadline_seconds`。计数跨角色、修复和重试共用。普通 completion 的网络重试只有执行器负责，首次发送就关闭 SDK 自动重试；没有可观察 HTTPX 发送的失败不自动重试，也不声称传输观测完整。取消或过期后保留已花费的用量，丢弃迟到结果。

账本区分逻辑调用、发送尝试、父调用与 operation/stage/prompt version，只保存模型/政策摘要，不保存请求正文或凭据。HTTPX 观察更新同一条发送记录；应用缓存命中与供应商缓存 token 分开。独立算法客户端将通过各自窄协议使用同一执行器。Agent、PageIndex 和 ChatIndex 的迁移分别验证，不代表技能及幻灯片生成等所有旧入口已迁移。

Agent answers use `ManagedAgentModel` with Agents SDK `ModelRetrySettings`.
The SDK retains responsibility for tool execution and replay safety. Every
attempt uses the task budget and disables underlying retries from the first
send. Raw streaming usage is preserved separately from SDK compatibility
counts, and stream cleanup completes before application leases are released.
