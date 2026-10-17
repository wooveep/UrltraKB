<div align="center">
  <img src="ultrakb/desktop/assets/brand/ultrakb-mark.svg" alt="UltraKB" width="88" />
  <h1>UltraKB</h1>
  <p>把原始资料编译为可阅读、可追溯、持续积累的本地知识库。</p>
  <p>原生桌面工作台 · CLI · REST API · PageIndex 树检索</p>
</div>

UltraKB 使用 LLM 将文档整理为相互关联的 Markdown Wiki，再通过问答、会话、
幻灯片和 Agent 技能使用这些知识。长资料采用 PageIndex 层级索引和基于推理的检索，
无需向量数据库。

原始文件、处理结果、来源位置和知识修订在本地保留。索引与存储在本地运行，
模型推理使用你配置的 LLM 服务；云端模型和可选 OCR 服务会接收对应的资料内容。

## 能做什么

- **归集多种资料**：PDF、Word（DOC/DOCX）、PowerPoint（PPT/PPTX）、
  Excel（XLS/XLSX）、Markdown、HTML、XML、CSV、纯文本和网页 URL。
- **积累 Wiki 知识**：生成摘要、概念、实体和交叉链接，跨文档整理同一主题。
- **回看原始依据**：按物理页、幻灯片正文与备注、内容块或工作表单元格读取来源，
  区分原文、生成的说明及尚未识别的内容。
- **维护变化**：保留来源与知识历史，重试未完成的导入，重编译已有处理结果，
  或按新策略重新处理原件；来源变化后可审阅并刷新受影响的知识。
- **生成输出**：带来源引用的问答、多轮会话、可分发的 Agent 技能、HTML 幻灯片和知识图谱。
- **选择入口**：桌面、CLI 和 API 使用相同的应用与存储逻辑。

Wiki 是普通 Markdown 文件，可以直接阅读，也可以把 `wiki/` 作为 Obsidian Vault 打开。

## 从源码开始

需要 Python **3.10–3.13** 和 [uv](https://docs.astral.sh/uv/)。
仓库包含 PageIndex、ChatIndex、ConDB 和 LiteLLM 的本地源码；请使用锁文件安装，
让它们与主程序一起加载。

```bash
git clone https://github.com/wooveep/UrltraKB.git UltraKB
cd UltraKB
uv sync --frozen --extra dev --extra desktop --extra api

# Linux / macOS：激活环境后，可在任意知识库目录使用命令
source .venv/bin/activate
```

Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1` 激活环境。
只使用 CLI 时，可以省略 `--extra desktop --extra api`。
使用 pip 或独立 wheel 的安装方法见[本地 SDK 指南](docs/vendor-sdk.md)。
当前 GitHub 仓库地址仍为 `wooveep/UrltraKB`，程序和 Python 包统一使用 `UltraKB` / `ultrakb`。

### 创建第一个知识库

```bash
mkdir -p ~/ultrakb-notes
cd ~/ultrakb-notes
ultrakb init --model gpt-5.4 --language zh
```

在知识库根目录创建 `.env`，填写模型服务凭据：

```dotenv
LLM_API_KEY=your-api-key
# 使用兼容端点时按需设置：
# OPENAI_API_BASE=https://your-gateway.example/v1
```

模型名采用 LiteLLM 的 `provider/model` 格式；OpenAI 模型可以省略前缀。
订阅或设备登录等认证方式按对应 provider 的要求配置，
详见[配置示例](examples/configuration/README.md)。

```bash
ultrakb add /path/to/paper.pdf
ultrakb add /path/to/documents/
ultrakb add https://example.com/article

ultrakb list
ultrakb query "这些资料的共同结论是什么？请给出来源。"
ultrakb chat
```

Word 和 PowerPoint 的版式转换需要项目固定的私有 LibreOffice 运行时。
源码环境的准备步骤见 [Office 导入指南](docs/office-ingest.md)；完整发行包应包含该运行时。
缺少转换资源时，按导入结果处理失败原因并重试。

## 桌面工作台

桌面使用 PySide6 / Qt Widgets，包含概览、资料、知识、会话、产物和任务页面，
支持知识库管理、来源阅读、流式回答、任务停止以及浅色、深色和跟随系统主题。

在仓库根目录准备本地渲染资源并启动：

```bash
uv run python scripts/prepare_desktop_assets.py
uv run ultrakb-desktop
```

资源准备需要 Rust **1.95.0** 和网络连接。Office 转换、嵌入附件恢复等资源
有各自的准备步骤，见[桌面指南](docs/desktop.md)和[构建指南](packaging/desktop/README.md)。
完整便携目录中的桌面入口名为 `UltraKB`，并附带 `UltraKBCLI`、`UltraKBAPI` 和
`UltraKBVerify`。支持的平台、构建条件及验收记录以构建指南为准。
Debian 安装包提供与 Python 安装相同的 `ultrakb`、`ultrakb-desktop` 和 `ultrakb-api` 命令。

## 常用命令

通过 `ultrakb --help` 或 `ultrakb <command> --help` 查看参数。

| 任务 | 命令 |
| --- | --- |
| 创建知识库 / 设置默认库 | `ultrakb init` / `ultrakb use /path/to/kb` |
| 查看状态 / 资料清单 | `ultrakb status` / `ultrakb list` |
| 导入文件、目录或 URL | `ultrakb add <path-or-url>` |
| 问答 / 多轮会话 | `ultrakb query "问题"` / `ultrakb chat` |
| 读取来源 | `ultrakb source SOURCE_ID` |
| 重试未完成的来源 | `ultrakb retry-source SOURCE_ID` |
| 查看 / 处理待办 | `ultrakb pending` / `ultrakb process-pending` |
| 重编译已有处理结果 | `ultrakb recompile DOC_NAME` |
| 预览重新处理原件 | `ultrakb reprocess SOURCE_ID` |
| 查看 / 刷新受影响知识 | `ultrakb refresh status` / `ultrakb refresh run` |
| 检查 Wiki 结构与语义 | `ultrakb lint` |
| 监控新资料 / 导出知识图谱 | `ultrakb watch` / `ultrakb visualize` |
| 从知识库生成 Agent 技能 | `ultrakb skill new NAME "技能目标"` |
| 生成 HTML 幻灯片 | `ultrakb deck new NAME "演示目标"` |
| 查看有效配置 | `ultrakb settings` / `ultrakb settings --global` |

可以在任意目录使用 `ultrakb --kb-dir /path/to/kb <command>` 指定知识库，
或通过 `ULTRAKB_DIR` 设置 CLI 使用的目录。
在会话中输入 `/help` 查看 `/add`、`/save`、`/skill`、`/deck` 等命令。

## 处理与存储

资料先被保留和规范化，再依据资料类型、长短规则和模型容量选择完整读取或分段索引。
默认 **10 个物理页及以内的 PDF 为短文档**；页数阈值可以配置。
短文档在已知模型容量不足时也会转为分段执行，资料长短与执行方式分别记录。
工作簿按工作表处理，幻灯片正文与备注分别定位。

模型调用总次数默认无固定上限，随每份文档的分段、标题核验和摘要等实际工作量增长。
重试、局部结构修复和每轮检索仍有各自的有限次数，手动停止和配置的截止时间仍会终止执行。
需要限制成本时可设置 `model_policy.max_calls` 为非负整数；`0` 禁止发送，
`null` 或省略该项表示无总次数上限。显式限额按逻辑任务累计，跨文档、阶段和重试共享；
将原有上限调大或改为 `null` 后续跑，会保留历史用量、检查点和原截止时间。

长文档的知识规划按模型容量检查完整请求。导航摘要超限时，规划改用完整标题、
节点位置和父子关系；原文证据和已经完成的索引、全文摘要仍可复用。
首次规划及其纠错请求都保留容量检查，不通过截断原文或遗漏目录节点绕过限制。
完整标题或已有知识目录仍超限时会分区处理，再统一页面身份；链接通过冻结的全量目录解析。
独立全文摘要窗口默认并发 2 路，仍受同一任务预算、停止信号和模型并发上限约束；
可在知识库配置设置 `document_summary_concurrency: 1` 恢复串行，已接受窗口继续复用。

编译结果发布到 Wiki。来源更新或移除后，相关页面会记录待刷新状态；
刷新使用当前有效的保留输入，并为需要审阅的人工修改保存差异。
历史知识继续关联当时实际使用的来源。
提交后通过公开指针发布不可变快照，导入期间桌面可继续浏览已公开知识。
HTTP Wiki 读取返回 `publication_id`，关联请求可携带该值固定同一版本；
页面编辑同时检查公开身份和正文版本，冲突时保留已有内容。

```text
my-kb/
├── .env                    # 本库模型凭据
├── .ultrakb/               # 配置、任务、来源修订、索引与知识历史
│   ├── config.yaml
│   └── format.json
├── raw/                    # 接入的原始资料
└── wiki/
    ├── AGENTS.md            # Wiki 结构与维护约定
    ├── index.md
    ├── log.md
    ├── sources/
    ├── summaries/
    ├── concepts/
    ├── entities/
    └── explorations/
```

全局配置位于 `~/.config/ultrakb/global.yaml`。
本库配置可覆盖全局设置；模型凭据按本库 `.env`、进程环境、全局 `.env` 的顺序解析。

```yaml
# .ultrakb/config.yaml
model: gpt-5.4
language: zh
pdf_short_max_pages: 10
concurrency: 5
```

可选配置包括角色模型、模型容量、调用预算、重试、超时、图片处理和 OCR 连接。
详见[配置示例](examples/configuration/README.md)、
[文档处理规则](docs/document-processing.md)及[来源阅读](docs/source-reading.md)。

## REST API

安装 `api` extra 后启动独立 HTTP 服务：

```bash
ultrakb-api --host 127.0.0.1 --port 7566
# 等价入口：python -m ultrakb.api
```

交互式接口文档位于 [http://127.0.0.1:7566/docs](http://127.0.0.1:7566/docs)。
服务提供知识库、导入、问答、会话、来源与产物接口，支持 SSE。

| 环境变量 | 用途 |
| --- | --- |
| `ULTRAKB_API_TOKEN` | 启用 Bearer Token 认证 |
| `ULTRAKB_KB_ROOT` | API 新建知识库的默认根目录 |
| `ULTRAKB_CORS_ORIGINS` | 配置允许的跨域来源 |
| `ULTRAKB_DISTRIBUTION_DIR` | 指定完整发行资料目录 |

未设置 Token 时允许本地无认证访问；对外提供服务前应设置认证。
请求示例和接口说明见 [REST API 指南](examples/rest-api/README.md)。

## 名称切换与旧数据

Python 包及 CLI 已从 `openkb` 切换为 `ultrakb`，桌面和 API 的命令分别为
`ultrakb-desktop` 与 `ultrakb-api`，环境变量前缀统一为 `ULTRAKB_`。
知识库状态目录改为 `.ultrakb/`，格式标识为 `ultrakb.context-kb`，
全局设置目录改为 `~/.config/ultrakb/`。

此次切换不会自动改写旧知识库。保留原库、原始资料和历史输出，在新目录使用
`ultrakb init`，然后重新导入原件；仅重命名 `.openkb/` 目录不能完成格式与内部引用迁移。
已有启动脚本、环境变量和 Agent 技能安装路径也需要同步。
固定 SDK 版本中的 `+urltrakb.*` 是依赖来源身份，继续按锁文件保留。

## 开发与更多文档

```bash
uv sync --frozen --extra dev --extra desktop --extra api
uv run pytest
uv run ruff check .
uv run mypy ultrakb
```

依赖 LLM 返回的测试直接调用真实 `deepseek-flash`，凭据读取仓库根目录的 `.env`，
不使用自定义模型成功回复 mock。真实模型验证覆盖编译、文档与会话索引、检索、
提示词和请求参数、响应解析、流式问答、多轮会话、工具调用、技能及产物生成交付。
容量、原文坐标、协议异常、纯 DTO 路由、持久化记录和故障用例直接验证本地业务边界；
这些用例不作为真实模型生成覆盖。真实生成链只发送必要的原文和上下文。
单次请求的 UTF-8 payload 上限为 64 KiB，输出默认 1024、最高 2048 token；上下文超限时明确失败并要求缩小夹具，不静默截断提示词。

每次 `pytest` 默认创建新的费用账本，按全部高峰价格准入的硬上限为 **2 元**，
空闲时段折扣不会增加可发送的额度；报告同时列出按实际时段核算的费用与高峰预留。
报告保存在
`.scratch/llm-test-runs/`。同一次回归及其重试应通过 `--llm-cost-report` 复用同一账本：

```bash
uv run pytest --llm-cost-report=.scratch/llm-test-runs/regression-01/cost.json
```

相同请求仅复用该次运行从真实模型取得的原始响应。费用按 API 回执中的实际 `usage`
和请求时段价格计算；缺少回执时保留费用预留。用例失败或预算不足会报告失败，
不通过跳过测试掩盖。开始下一次完整验证时使用默认新账本或新的报告路径。
早期按请求时段准入的旧账本仅用于费用回顾，不能继续付费验证。

仓库约定见 [AGENTS.md](AGENTS.md) 和[工程原则](docs/golden-principles.md)。
依赖使用固定版本，知识库写入由统一的锁和 mutation 层保护。

- [桌面使用](docs/desktop.md) · [构建与发行](packaging/desktop/README.md)
- [本地 SDK 与来源校验](docs/vendor-sdk.md)
- [Office 导入](docs/office-ingest.md) · [工作簿导入](docs/workbook-ingest.md)
- [连续文本与来源回读](docs/content-block-readback.md)
- [重编译与重新处理](docs/reprocessing.md) · [知识刷新](docs/knowledge-refresh.md)
- [待办导入与附件发现](docs/pending-imports.md)
- [命令示例](examples/commands/README.md) · [会话示例](examples/chat/README.md)
- [Agent 技能](skills/ultrakb/SKILL.md) · [幻灯片示例](examples/slides/README.md)

## 来源与许可

UltraKB 基于 [VectifyAI 的 OpenKB](https://github.com/VectifyAI/OpenKB) 开发，
并使用 PageIndex、ChatIndex、ConDB、LiteLLM 等项目。
原始源码的 Apache-2.0 许可和版权声明见 [LICENSE](LICENSE)。

便携发行包含分别许可的第三方组件，包括 PyMuPDF / MuPDF；当前组合发行采用
AGPLv3 路线，并配套提供对应源码、构建资料和第三方声明。
分发要求与资料清单见[发行说明](packaging/desktop/DISTRIBUTION.md)。
