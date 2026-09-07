# 完整依赖便携运行：Debian 实测与 Windows 待验收

2026-09-07。对应 [原型：完整依赖与工作进程能否在目标系统免安装运行？](https://github.com/wooveep/UrltraKB/issues/7)。

**采用 PyInstaller onedir + 显式 spawn，Windows 11 与 Debian 13.6 GNOME/X11 x64 使用同一构建方向。Debian 已得到真实冻结包证据和用户观看确认；用户另明确“Windows 11 完整包同样采用该方案即可；继续后续决策”。本票据此完成方案选择，Windows 实测保留为后续构建与交付验收门槛，不把选型确认写成运行通过。**

程序代码与依赖资源足以执行本轮代表性任务，不需要为这些结果切换 Nuitka。
这不是完整产品验收，也不是所有模型提供方、文档和平台功能的完整兼容证明。
用户此前“已验证无问题”针对另一张渲染原型票，不能替代本轮完整包证据。
本轮用户观看便携原型后回复 **“没有问题”**，已记录为当前 Debian 原型的观看确认；不据此推断已执行尚未构建的 Windows 完整包。详见 [用户反馈](evidence/user-feedback.json)。

## 已得到的证据

| 项目 | 实测结果 | 边界 |
| --- | --- | --- |
| 干净运行 | Debian 13.6 slim 普通用户，网络 none，真实 Qt 6.11.2/xcb；PATH 中 python/python3/node/cargo/gcc 全为空 | 共享 Debian 宿主内核和 GNOME/X11；未用另一台独立机器 |
| 解压移动 | 最终 tar.gz 解压到新目录，10,755 个文件/链接逐项校验；40 个符号链接及执行位保持 | 使用 Python tarfile，非用户文件管理器；data filter 收紧普通写权限 |
| 程序与数据分离 | 程序目录只读，含中文和空格；cwd `/tmp`，测试 KB `/data/测试结果`，核心全局配置沿用 `/home/probe/.config/openkb/global.yaml` | 交互演示默认使用 scratch 配置，不触碰真实用户注册表 |
| 文档真实转换 | Markdown、带图中文 PDF、DOCX/PPTX/XLSX/XLS/HTML 共 7 类文件转换；第二次输入去重；20 页 PDF 进入长文分支 | 没有云端 PageIndex 调用；不评价任意真实文档 |
| 原生依赖 | Magika 对 PPTX 实际推理，ONNX Runtime 1.20.1；随包 tiktoken 词表实际编码 | 非所有 ONNX provider 或 tokenizer |
| 核心资源 | 生成两节点一关系图谱 HTML；3 个内置 Skill 与 prompt 可读；trafilatura 实际正文提取 | 未完整运行模型驱动的 Skill/deck 生成，也未访问真实 URL |
| 模型传输 | LiteLLM 流式接收及 Agents/OpenAI SDK 一次请求成功 | 本机 HTTP 虚拟响应；无真实模型、TLS、代理质量结论 |
| 随包渲染 | 32 样本 × 2 主题：60 次正常 PNG、4 次明确预期错误；Node/helper 从 bundle 绝对路径启动；Qt 最终显示 PNG | 本轮 2× 栅格；四档缩放语义证据沿用渲染原型，非新一轮逐样本人工评分 |
| 进程 | A/B 两库进程并行，模型配置分别为 A/B；故意退出码 23 后核心 journal 恢复；20 项写入在第 1 项后停止，保留已完成结果 | 不代表跨入口排队、完整会话互斥或任意任务可立即取消 |
| 关闭和退出 | 任务运行中关窗，无托盘时保持可见并完成任务；7 个工作进程全部回收；容器进程正常返回 0 | 有托盘的隐藏/恢复路径尚未得到环境实测 |

机器结果：[干净包运行](evidence/debian-clean-summary.json)、[Qt 截图](evidence/debian-clean-window.png)、[控制台](evidence/debian-clean-console.log)。
截图由真实 `QWidget.grab()` 保存，未使用浏览器或合成图代替。
`all_expected` 只汇总这些探针的预定结果；监听原子重命名缺口另列，不能把它解读为完整功能通过。

## 采用方向和代价

- **构建**：PyInstaller 6.22.2、hooks-contrib 2026.7，目录模式；CPython 3.12.13；PySide6-Essentials/shiboken6 6.11.2。保留现有核心依赖 pins。
- **入口**：冻结入口先执行 `freeze_support()`，再加载 Qt。标准库显式 `spawn` 将重依赖移到工作进程，Pipe 回传事件，父进程逐一 join。
- **资源**：从真实 wheel 收集 prompts/templates/内置 Skill；在 `_internal/probe_assets` 放独立 Node/helper、显式字体、词表及原型样本。最终产品需单独维护资源清单。
- **外部 helper**：Linux 启动前移除 PyInstaller 注入的库路径并恢复原值；Windows DLL 搜索路径仍需真实验证。
- **体积**：tar.gz **342,032,335 bytes（326.19 MiB）**，常规文件约 **794.57 MiB**；目前优先完整依赖，没有做激进裁剪。含用于验证的样本及全部 MathJax 资源，不是最终产品尺寸承诺。
- **耗时**：本轮完整探针约 23 秒，包含 64 次 renderer 调用；这不是 OS 冷缓存启动基准。开发机已有文件缓存，不报告虚构冷启动数字。

精确构建输入：[核心哈希锁](core-requirements.txt)、[GUI/构建哈希锁](tooling-requirements.txt)、[构建日志](evidence/pyinstaller-build.log)。
最终产物：[文件/链接清单](evidence/bundle-files.json)、[压缩包 SHA256](evidence/archive.json)、[解压校验](evidence/extraction.json)、[随包资源哈希](evidence/asset-hashes.json)。
[bundle-distributions.json](evidence/bundle-distributions.json) 是实际收集到的 Python distribution 元数据，**不是所有随包模块或原生库的完整 SBOM**；部分 Office/Qt 模块存在而元数据不在这个列表，版本以构建输入和运行证据对照。
[选定 ELF 检查](evidence/selected-elf.json) 包含主程序、Python、xcb、ONNX、MuPDF、Node/helper 共 9 项，在干净容器中均无未解析依赖；未据此声称所有可选 Qt 插件均验证。

## 原型揭示的问题

1. **Agents SDK 数据不会自动全部入包。** 初版在读取 `agents/sandbox/memory/prompts/memory_consolidation_prompt.md` 时失败；显式收集 `agents` 数据后，真实 SDK 调用通过。不能只依赖 Python import 静态分析。
2. **关闭事件会拦住真正退出。** 初版一律 ignore closeEvent，导致任务结束后 quit 仍不退出；只在“确实退出且工作进程已回收”分支允许关闭后，最终包正常退出。用户关窗隐藏与真正退出必须分开。
3. **现有 watcher 漏掉原子替换。** `atomic_write_text` 的隐藏临时文件重命名不会进入现有 on_created/on_modified 处理；本轮 `atomic_rename_observed=false`。普通新建文件可观测，observer 可以 stop/join。未改产品 watcher，后续应在 [决策：任务、配置快照和目录监听应如何衔接生命周期？](https://github.com/wooveep/UrltraKB/issues/9) 明确移动事件、去重与重扫策略。
4. **嵌套原型目录可能把自己的运行时打进输入 wheel。** 临时 wheel 构建区过滤 `.runtime/.build/.venv`，产品文件保持原样；输入 wheel 降为约 414 KiB，最终资源仅从显式 bundle 路径进入。

## 尚未得到的结论

- Windows 11 原生构建、VC/DLL 完整性、同批任务、托盘恢复、退出/异常/残留进程、EXE 控制台行为；[README 的 Windows 部分](README.md#windows-构建与待验收步骤) 提供待验证命令，不将它们表述为已通过。
- 当前宿主 GNOME 的 Qt 同样报告无可用托盘。已验证回退，未安装或更改桌面扩展去制造“有托盘”证据。
- 真实模型与网络端点、代理/TLS、云端 PageIndex、复杂 Skill/deck 生成、完整 Markdown 工作台和 CLI/API 全面兼容检查不在本轮成功计数内。生产入口、源码和依赖锁未改动。
- 任务快照、安全取消点、跨入口等待和完整会话锁继续由各自决策票确定；不把本原型 Pipe/Event 写成已批准的公共用例契约。
- 便携包正式再分发材料仍依赖 [决策：便携包中的 PyMuPDF/MuPDF 采用怎样的再分发安排？](https://github.com/wooveep/UrltraKB/issues/11)。本轮仅本地技术包，没有发布产品二进制或正式流水线。
- 本轮用户已反馈“没有问题”，并明确 Windows 11 同样采用该方案、继续后续决策；Windows 完整包验证环境仍需在后续构建时落实。此前渲染票的反馈不移用到此票。

两平台构建方向已按用户最新选择确定。Windows 后续原生构建必须验证缺库、完整依赖、同批任务、托盘与退出；未通过前不宣称 Windows 交付验收完成。这些门槛交由 [决策：按什么迁移顺序与验收门槛交给实施？](https://github.com/wooveep/UrltraKB/issues/10) 落入实施路线。若发现资源/DLL 问题，先修正此方案并复测；当前无需另建一套 Nuitka 原型。
