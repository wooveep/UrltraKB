# 原生桌面便携包与隔离工作进程：构建证据

研究日期：2026-09-07。对应决策票：[研究：便携包与隔离工作进程有哪些可验证的构建方案？](https://github.com/wooveep/UrltraKB/issues/4)。范围依据：[Spec: Native desktop workbench with CLI/REST compatibility and portable Windows/Linux releases](https://github.com/wooveep/UrltraKB/issues/1)。

## 结论与决策边界

推荐首先验证 **PyInstaller `onedir` + CPython 3.12 系列 + PySide6/Qt Widgets + 标准库 `multiprocessing` 的显式 `spawn` 工作进程**。Windows 11 x64 和 Debian 13.6 GNOME/X11 x64 分别原生构建，输出 ZIP 与保留符号链接的 tar 压缩包。程序依赖随包，配置、知识库沿用原有位置。这个选择是进入原型的默认方案，不是打包已成功的结论。

备选为 **pyside6-deploy/Nuitka `standalone`**：只有在默认方案出现无法合理维护的动态导入、原生库收集问题，或测得无法接受的启动/任务性能后，才用同一证据集比较。不要因某工具展示过空窗口就切换。两条路线都需要资源清单与实际任务验证；本次没有安装、构建、运行原型或修改生产依赖。

独立的再分发决策已经明确：现有 PDF 路径使用 PyMuPDF/MuPDF，需确定满足其开源条款的分发安排或商业许可安排。它不阻止本地技术验证，但最终发布路线不能把仓库的 Apache-2.0 声明当作所有随包组件的许可结论。

## 候选为何可验证

| 候选 | 一手事实 | 对本项目的判断与未验证部分 |
| --- | --- | --- |
| PyInstaller `onedir` | hooks 可指定隐藏导入、数据、动态库与包元数据；运行时提供冻结多进程分流。当前 stable 文档版本为 **6.22.2**。[hooks](https://pyinstaller.org/en/stable/hooks.html)、[多进程说明](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#multi-processing) | 目录产物便于定位遗漏文件、检查 Qt 模块与替换随包 helper；无需先引入全项目 C 编译过程。6.22.2 是研究候选，尚未审定为项目 pin。hooks-contrib 也需单独精确锁定，不能默认其每次升级都等价。 |
| pyside6-deploy/Nuitka `standalone` | Qt 工具包装 Nuitka，默认是 `onefile`，可改为 `standalone`；可指定 Qt 模块、插件和额外参数。当前 Qt 文档默认 Nuitka 为 **4.1.1**；工具会安装构建依赖。[Qt 部署工具](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html) | 必须在专用构建环境中显式选择目录模式、锁定工具依赖，审查实际生成命令。C11 编译器与包配置增加排障面；不代表动态依赖自动完整。 |
| Nuitka 的收集边界 | standalone 才提供脱离 Python 安装的产物；隐藏 Python 模块、数据和 DLL/EXE 分属不同收集机制，不能把代码/二进制一律当数据文件。[官方手册](https://nuitka.net/user-documentation/user-manual.html) | 备选原型仍须逐项证明模型库、ONNX、Qt 插件、外部 renderer 可用；不能以“编译成 C”推定兼容性或性能。 |

CPython 3.12 系列是保守的原型起点：仓库声明支持它，锁文件列有 ONNX Runtime 1.20.1、NumPy 2.4.6、Pandas 3.0.3、Pillow 12.2.0、lxml 6.1.1 的 CPython 3.12 Windows x64/Linux x64 wheels。**有 wheel 不等于这些版本组合运行正确**。Python 补丁版本、PySide6/Essentials/Addons/Shiboken6 的同批精确版本、打包工具与辅助运行时，必须在原型构建清单中锁定并记录下载哈希；本报告不凭滚动文档替项目锁定 Qt 版本。[现有 pyproject](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/pyproject.toml)、[现有 uv.lock](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/uv.lock)

## 真实依赖闭包：不能只冻结 Qt 壳

基线源码为 `0cec254bb2f37adff8b1af2e1bd2802ea56910ee`。`pyproject.toml` 的 **14 个核心直接依赖**均精确固定：PageIndex 0.3.0.dev3、MarkItDown[docx,pptx,xlsx,xls] 0.1.5、trafilatura 2.0.0、Click 8.4.0、watchdog 6.0.0、LiteLLM 1.87.2、openai-agents 0.17.3、openai 2.44.0、PyYAML 6.0.3、python-dotenv 1.2.2、json-repair 0.59.10、prompt_toolkit 3.0.52、Rich 15.0.0、portalocker 3.2.0。构建依赖及部分 extras 当前没有精确 pin；不能把“核心直接 pins”说成完整可重现环境。

`uv.lock` 有 **135 个跨版本/平台条目**，不是 135 个必装运行时包。只遍历核心及其启用的 Office extras、按 CPython 3.12 条件计算，Linux 可达 115 个外部包，Windows 可达 119 个；这是锁图静态读取，没有运行依赖求解器或安装校验。GUI 增量不在这个基线内。后续以每个平台实际解析、下载哈希和最终产物 manifest 核对闭包，而不是把开发环境整体复制进去。[依赖与锁文件](https://github.com/wooveep/UrltraKB/tree/0cec254bb2f37adff8b1af2e1bd2802ea56910ee)

| 路径 | 现有锁定事实与资源 | 必须触发的验证 |
| --- | --- | --- |
| PDF / PageIndex | PageIndex 传递引入 **PyMuPDF 1.27.2.3、PyPDF2 3.0.1**；本项目 `converter.py` 与 `images.py` 直接 import pymupdf，短 PDF 本地转图文，长 PDF 可调用云服务后本地补全。[转换调用](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/openkb/converter.py#L135)、[图像路径](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/openkb/images.py) | 含图中文 PDF 与长文分支，检查 MuPDF 本地扩展/共享库。云端 PageIndex 与模型服务仍需网络和配置，程序便携并不把这些服务变成本地。 |
| Office / 文件识别 | MarkItDown 0.1.5 → **Magika 0.6.3 → ONNX Runtime 1.20.1 + NumPy**；Office extras 带 mammoth 1.11.0、python-pptx 1.0.2、openpyxl 3.1.5、xlrd 2.0.2、Pandas，以及 lxml/Pillow。Magika 按包内路径读取 `model.onnx`、模型 JSON 和内容类型 JSON。[MarkItDown 元数据](https://github.com/microsoft/markitdown/blob/v0.1.5/packages/markitdown/pyproject.toml)、[Magika 资源路径](https://github.com/google/magika/blob/python-v0.6.3/python/src/magika/magika.py#L93) | 每种 Office 格式实际转换，第一次调用文件识别，验证 ONNX 模型及 provider DLL/SO 都存在。现有惰性导入注释允许某些裁剪包删 Office；本次完整日常流程不能据此裁掉它。 |
| 模型与 HTTP | LiteLLM、Agents/OpenAI、tiktoken 0.13.0、tokenizers 0.23.1、pydantic-core 2.46.4、cryptography 48.0.0 等含动态导入/原生模块；LiteLLM 带模型元数据与 tokenizer 数据；tiktoken 源码也包含按 URL 加载词表的路径。[LiteLLM 数据](https://github.com/BerriAI/litellm/tree/v1.87.2/litellm/litellm_core_utils/tokenizers)、[tiktoken 词表加载](https://github.com/openai/tiktoken/blob/0.13.0/tiktoken_ext/openai_public.py) | API Key、受支持 provider、兼容端点、流式请求、首次 tokenizer 调用、TLS/代理，清空开发机缓存后重试。收集需要的 namespace、元数据、证书与词表；不能只测一个 provider 就声称所有既有 provider 等价。 |
| URL / 本地监控 | trafilatura → lxml 与提取规则；watchdog 使用平台后端；portalocker 保留跨进程锁。[锁文件](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/uv.lock) | URL 提取、中文路径目录监听、同库锁等待、跨库并发，分别在两平台执行。 |
| Prompt / Skill / HTML 产物 | Markdown prompts 从磁盘加载；图谱模板由包资源读取；wheel force-include 三个内置 Skill 目录，包含主题和引用资料。[prompts](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/openkb/prompts/__init__.py)、[图谱模板读取](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/openkb/visualize.py#L76)、[Skill 发现](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/openkb/agent/skills.py#L50) | 从构建后的 wheel 收集资源，实际生成 Skill、deck、graph，验证主题/引用可读；不依赖源码 checkout。保留 HTML 产物资源，排除已退役 React 产品和 QtWebEngine。 |

外部程序应按实际分支区分：现有 Office extras 用 Python 转换库，没有证据要求用户安装 Microsoft Office/LibreOffice。MarkItDown 的 audio-transcription 是另一个未启用 extra，不能把 FFmpeg 当作当前 Office 流程的必需项；其内置初始化会寻找可选 ExifTool，插件默认不启用，当前 `MarkItDown()` 也未启用插件。Skill marketplace 读取 Git 用户信息失败会使用占位值，Git 不是该步骤的硬启动依赖。[MarkItDown 初始化](https://github.com/microsoft/markitdown/blob/v0.1.5/packages/markitdown/src/markitdown/_markitdown.py#L99)、[extras](https://github.com/microsoft/markitdown/blob/v0.1.5/packages/markitdown/pyproject.toml)、[Git 回退](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/openkb/skill/marketplace.py#L32)

CLI/API 的正常 pip 安装继续无 Qt；未来 desktop extra 与独立构建锁增加 Qt/renderer，api extra 直接表达 REST 依赖。注意 `openai-agents → mcp` 已传递带入 starlette、uvicorn、python-multipart：桌面未启动 API 不代表这些包能任意删掉。应按实际导入边界收集，不把 FastAPI 服务当 GUI 的前置入口。[当前锁文件](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/uv.lock)

## 冻结工作进程与发现路径

首个原型采用顶层可导入 worker 函数、可序列化任务描述与 `multiprocessing.get_context("spawn")`。GUI 启动入口在 Qt、CLI 参数处理及重量库初始化之前调用 `freeze_support()`；PyInstaller 提供的覆盖实现在 **Windows 和 Linux 都需要**。否则重新执行桌面二进制可能启动第二个 GUI、误解析内部参数或递归拉起进程。不要把冻结后的 `sys.executable` 当成通用 Python 去执行 `-m openkb...`；它是程序 bootloader。[PyInstaller 分流规则](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#multi-processing)、[运行时路径](https://pyinstaller.org/en/stable/runtime-information.html#using-sys-executable-and-sys-argv-0)、[Python spawn 约束](https://docs.python.org/3.12/library/multiprocessing.html#the-spawn-and-forkserver-start-methods)

这是推荐的机制，不提前决定完整 IPC 协议。若需要 `QProcess` 或更独立的生命周期，可在后续决策比较专用 frozen worker 可执行文件或明确的内部模式分流；它们是应用自己的协议，不能声称 `freeze_support()` 会自动处理任意命令。

建议原型目录契约为：桌面启动器；PyInstaller `_internal` 运行库与按包相对路径保存的资源；明确的 renderer 目录（Node、MathJax 模块、Merman/字体 helper）；字体和许可证目录。通过 bundle 根与 `__file__`/`sys.executable` 的已知语义定位，helper 一律绝对路径启动，显式传入任务目录与环境。缓存与日志写用户可写位置，知识库写用户选择的目录；不修改 GUI 主进程 cwd 或系统全局环境来切库。[PyInstaller 资源路径](https://pyinstaller.org/en/stable/runtime-information.html#using-file)

Qt 插件使用收集后的相对布局和 `qt.conf`/应用内 library paths；不要要求用户设置系统级 `QT_PLUGIN_PATH`。Windows 验证 `qwindows`，Debian/X11 验证 `xcb` 及实际 imageformats/platformthemes/SVG 等使用到的插件，记录动态库加载路径。仅收集 Widgets 所需模块，并扫描产物和进程树确认没有 QtWebEngine、Chromium 或浏览器 helper。[Qt 插件部署](https://doc.qt.io/qt-6/deployment-plugins.html)

随包 Node 和原生渲染器属于受控 helper；外部默认浏览器属于系统程序，不能共用未经检查的库搜索环境。PyInstaller 会修改 Linux 的 `LD_LIBRARY_PATH` 和 Windows DLL 查找目录，外部程序可能因此加载错误共享库。针对每类子进程单独验证环境处理；避免多线程 GUI 中随意反复修改进程级 DLL 路径。Windows windowed 模式的标准流可能为 `None`，启动异常和任务日志必须仍可获取。[外部程序与无控制台说明](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)

关闭窗口后 GUI 主进程继续托盘保活，工作进程不需要变成独立守护服务。显式退出才等待或安全停止并回收 worker/renderer；测试中应证明无孤儿进程、无重启自动续跑、运行中配置固定。具体事件与停止边界留给任务生命周期决策，打包原型只需证明机制可承载它们。

## 两个平台仍依赖什么

| 环境 | 构建与运行边界 | 原型必须记录 |
| --- | --- | --- |
| Windows 11 x64 | 在 Windows 原生构建。若用 Nuitka，准备匹配 Python 位数的 C11 工具链；pyside6-deploy 使用 MSVC 的 dumpbin 做依赖检查。ONNX Runtime 官方指出 Windows 构建需要 VC++ 2019 runtime。Microsoft 支持运行库应用本地部署，但可再分发文件范围仍要按其条款确定。[Nuitka 要求](https://nuitka.net/user-documentation/user-manual.html#requirements)、[Qt 工具依赖](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html#considerations)、[ONNX 要求](https://onnxruntime.ai/docs/install/#requirements)、[Microsoft 部署](https://learn.microsoft.com/en-us/cpp/windows/deployment-in-visual-cpp?view=msvc-170) | 干净普通用户环境，不预装 Python/Node/Qt/开发工具/额外 VC Redistributable；逐项识别缺 DLL，验证允许随包的运行库。不能以要求用户先安装 VC 运行库来通过“解压启动”验收。 |
| Debian 13.6 GNOME/X11 x64 | 在目标 Debian 原生环境构建/验收。PyInstaller 不打包 glibc；Qt xcb 需要 X11/XCB、xkbcommon、字体等库，GNOME/D-Bus/显示服务仍由 OS 提供。Qt 的当前平台表未列 Debian 13，不能直接外推支持；Qt Online Installer 的基线也不能当成 PySide wheel 的实际 ELF 基线。[PyInstaller Linux](https://pyinstaller.org/en/stable/usage.html#making-gnu-linux-apps-forward-compatible)、[Qt X11 库](https://doc.qt.io/qt-6/linux-requirements.html)、[Qt 平台表](https://doc.qt.io/qt-6/supported-platforms.html) | 对最终 ELF/插件/helper 用 readelf/ldd 等检查未解析依赖、glibc/GLIBCXX 需求；与干净目标 OS 库清单比对。需额外的可分发用户态库应随包，不能悄悄依赖构建机安装过的 `-dev` 包。不承诺其他发行版、无图形服务器或 noexec 文件系统。 |

Linux PyInstaller 6 目录包使用符号链接；压缩与解压必须保留链接及执行位，首选 tar 系列压缩，测试实际文件管理器解压结果。Qt 托盘依赖桌面提供 StatusNotifier/XEmbed 等支持，GNOME 某些激活行为受扩展影响；同时验证托盘可用路径和不可用时窗口保持可见的既定回退，不能只依据 `isSystemTrayAvailable()` 文档承诺目标桌面体验。[符号链接要求](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#requirements-imposed-by-symbolic-links-in-frozen-application)、[Qt 托盘](https://doc.qt.io/qt-6/qsystemtrayicon.html)

Node/MathJax/Merman 的候选版本与字体控制由并行的[研究：无浏览器渲染候选应使用哪些版本、输出通道与随包资源？](https://github.com/wooveep/UrltraKB/issues/3)报告负责，当前候选为 Node **24.20.0**、MathJax 源包和 New Computer Modern 字体包 **4.1.3**、Merman **0.7.0**。包级原型应验证它们在干净机器通过绝对路径发现、完全使用本地资源并将结果交给最终 Qt 控件。[Node 固定版目录](https://nodejs.org/download/release/v24.20.0/)、[Node 摘要](https://nodejs.org/download/release/v24.20.0/SHASUMS256.txt)

特别是“把字体放在程序旁边”不能证明 renderer 已加载它：Merman 0.7.0 的 PNG 路径调用 `load_system_fonts`，其 CLI 未提供字体目录参数。需在渲染原型中选择可控的 Qt 字体注册/SVG 通道，或能向 fontdb 注入字体的 Rust helper；包级原型复验所选机制。字体包分发材料的缺口由渲染研究记录，须与最终 bundle 清单合并。[Merman PNG 字体加载](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/src/render/raster.rs#L601)、[CLI 参数](https://github.com/Latias94/merman/blob/v0.7.0/crates/merman-cli/src/cli.rs)

## 再分发依据与新增决策

| 组件 | 已核对的一手事实 | 本地图应处理什么 |
| --- | --- | --- |
| PyMuPDF/MuPDF | 锁定版 PyMuPDF **1.27.2.3** 的 COPYING 是 AGPLv3；官方说明同时提供商业许可。它由 PageIndex 传递引入，PDF 核心直接使用。[固定版 COPYING](https://github.com/pymupdf/PyMuPDF/blob/1.27.2.3/COPYING)、[双授权说明](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)、[锁定来源](https://github.com/wooveep/UrltraKB/blob/0cec254bb2f37adff8b1af2e1bd2802ea56910ee/uv.lock) | 新增 HITL 问题：选择哪种可满足条款的开源分发安排，或商业许可安排？如果均不接受，再讨论 PDF 依赖替换对核心兼容约束的影响。本报告不自行改项目许可，也不推断整个应用必须采用某许可或判定现有仓库违规。 |
| PySide/Qt | Qt for Python 提供 LGPLv3/GPLv3 与商业许可；部分 Qt 模块并非 LGPL；还包含第三方许可材料。[PySide](https://doc.qt.io/qtforpython-6/index.html)、[Qt 模块许可](https://doc.qt.io/qt-6/licensing.html)、[第三方材料](https://doc.qt.io/qtforpython-6/licenses.html) | 按实际随包模块/动态库记录许可、来源及相应分发材料，不能只写“Qt 免费”。目录包使动态组件可见，但目录形式本身不是合规证明。 |
| PyInstaller / Nuitka | PyInstaller 为 GPL 加构建产物例外，少量文件 Apache-2.0。**Nuitka 4.1.1 为 AGPLv3 加 Runtime Library Exception**，该例外允许独立模块通过编译形成的产物选择条款；不能套用旧版 Apache 描述，也不能忽略例外。[PyInstaller](https://pyinstaller.org/en/stable/license.html)、[Nuitka 固定版说明](https://github.com/Nuitka/Nuitka/blob/4.1.1/README.rst#license)、[Runtime 例外](https://github.com/Nuitka/Nuitka/blob/4.1.1/LICENSE-RUNTIME.txt) | 构建工具与产物内运行库分别登记；切换工具时更新材料。 |
| CPython、Node、模型/字体与原生库 | Node 主许可证允许再分发并要求保留声明，LICENSE 另含第三方条款；其他文件须以最终选定版本的官方材料为准。[Node LICENSE](https://github.com/nodejs/node/blob/main/LICENSE) | 随包许可证/NOTICE 与产物 manifest 对齐；字体和模型数据也在范围内。Node/Merman/MathJax 版本由渲染研究与原型统一，不能只记 npm 顶层包名。 |

## 原型的最小证据集

1. **可复建来源**：源码 commit、Python/Qt/freezer/hooks/renderer 精确版本，目标 OS/架构、构建命令、锁文件、下载 SHA256、构建日志、最终文件与许可证清单。无隐式 `latest`；不改现有核心直接 pins 来掩盖冻结故障。
2. **干净启动**：两个目标 OS 上由文件管理器解压/启动，普通用户，无 Python/Node/Qt/开发环境；目录含空格、中文，改变启动 cwd，迁移程序目录后仍能运行。首个窗口、无控制台异常日志、产物体积和冷启动耗时有记录。
3. **真实依赖触发**：Markdown/含图 PDF/DOCX/PPTX/XLSX/XLS/URL、长文路径、至少受支持典型 API Key provider/兼容端点、流式任务、Skill/deck/graph。清空 tokenizer 等开发缓存，记录不属于模型请求的首次联网行为；不能把“import 成功”当任务成功。
4. **进程与配置证据**：连续启动多个任务，进程树仅有期望 GUI/worker/renderer；不同库并行无配置串用，同库等待；切库、关窗托盘、显式退出、失败与协作停止均能回收资源；Windows 无递归窗口或控制台闪烁。任务正确性的深层协议仍由对应决策票确定。
5. **最终渲染控件**：在冻结应用内使用约定公式与九类图种样本，中文、缩放、字体与本地图片正确。断开非必要网络、移除系统 Node，仍可渲染；无浏览器引擎文件/进程。Merman 字体机制与 Qt 最终显示都必须通过。
6. **系统边界与兼容**：记录 Windows DLL、Linux ELF/插件加载与 OS 提供项；GNOME 托盘有/无两条路径；HTML 产物外部浏览器预览后主程序仍正常。另用既有正常 Python 安装运行 CLI/API 兼容检查，证明它们无需 Qt、桌面无需 REST 服务。
7. **提交用户判断的材料**：两平台结果矩阵、实际截图/录屏、进程/缺库/资源日志、失败与限制、候选体积/启动比较。依此决定采用默认方案、修复后重测或触发 Nuitka 备选；资料研究的关闭不代替用户确认原型，也不构成 Windows/Debian 交付已验收。

研究期间仅读取仓库、锁文件和一手资料，执行静态依赖图计数；未修改代码、安装依赖、构建包或运行产品测试。
