# UltraKB workbench: design and source acceptance

This implements [issue #12](https://github.com/wooveep/UrltraKB/issues/12).
The accepted Qt Widgets, shared application interfaces, isolated task workers and
browser-free rendering architecture remain in place. The desktop display name is
**UltraKB**. Python packages and CLI entry points use `ultrakb`, KB state lives in
`.ultrakb`, and Qt settings use the `UltraKB/UltraKB` identity. For older data,
follow the [name-switch guidance](../README.md#名称切换与旧数据).
Portable entry points and archive roots use `UltraKB`; original third-party notices are retained.

## Composition

- `brand.py` and bundled original SVGs define the display identity and artwork.
  Asset provenance is in `ultrakb/desktop/assets/brand/README.md`.
- `appearance.py` applies a shared palette and native control styling. Follow
  System is the initial choice; explicit Light/Dark and sidebar preference are
  stored as application-local Qt settings, separately from credentials and KBs.
- `shell.py` owns the compact top bar, 224/64 logical-pixel navigation and page
  selection. Automatic compact mode below 1080 logical pixels never persists over
  the wide-window preference; its toggle remains reachable.
- `workspaces.py` composes Overview, Documents, Knowledge, Conversations, Artifacts,
  Tasks and Settings using existing controls. Common management forms support
  page embedding; Escape cannot dismiss an embedded page. Destructive confirmation,
  conflict review, recovery and detailed task inspection retain their dialogs.
- `fonts.py` loads the supplied `assets/fonts/` files into the application only.
  Source Han Sans CN 2.005 uses the supplied static Regular (400), Medium (500)
  and Bold (700) faces. Source Code Pro 2.042 supplies the same code weights;
  its 1.062 Italic/Bold Italic faces preserve emphasized code. The base QFont
  leaves its style name and variable axes unset so QSS and Markdown inherit
  real weights, with Source Han Sans as the explicit Chinese code fallback.
  Only manifest-listed faces and notices are included in wheels and runtime
  archives; unused reference fonts stay in the source workspace.
  Windows desktop entry points select Qt's bundled FreeType font engine for
  consistent variable-font rasterization. Explicit Qt platform arguments or the
  `QT_QPA_PLATFORM` environment setting take precedence; system settings are untouched.
- The flat, neutral shell uses a full-height navigation rail and a quiet context
  bar. Conversations use a centered readable column, distinct user messages and a
  bottom composer. Enter sends, Shift+Enter inserts a line, and IME confirmation
  does not send a message.
- `drawer.py` keeps conversation history and page sources in local overlays with
  an interruptible 180 ms fade/slide. Escape and outside clicks close a drawer and
  return focus; changing workspaces or KBs retires it without losing drafts.
- Knowledge directory and page sources are local, optional panes. The reading
  and editing controls persist while navigating, so toggles preserve drafts.
  Task-owned outputs are displayed exclusively with their task's full KB location.
- Tasks and watches remain application-wide. Task submission does not change the
  current page. Global status leads to details and manual retry; shutdown selects
  Tasks and leaves observation and cooperative stop usable while disabling new work.

## 首次配置与可用性复核（2026-10-15）

首次启动且没有已登记知识库时，打开独立的五步向导：**选择服务商 → 连接服务 →
选择与测试模型 → 设置知识库 → 确认创建**。已有知识库使用三步连接向导，直接编辑、
验证当前知识库实际使用的连接。向导不再嵌入高级设置页，也不再同时展示全局和本库两套表单。
流程组织参考 [Hermes 的模型配置入口](https://github.com/NousResearch/hermes-agent/blob/main/hermes_cli/setup.py)。

- 已验证的全局连接直接复用，新建知识库从知识库参数开始。只有最终点击“创建知识库”
  才创建目录；创建后进入“资料”，引导导入第一份资料。
- 自定义服务填写 API 地址和 Key；模型步骤可主动获取服务的模型列表，服务未提供列表时
  仍可手填名称。获取列表不保存草稿、不发送知识库内容。模型在连接测试通过后才标记可用。
- “保存并测试连接”明确区分保存与验证；失败留在当前步骤，显示可重试的说明。
  返回保留输入，取消保留已保存的设置。也可以显式选择稍后连接、先创建空库。
- 新知识库继承全局连接；已有库的补配置作用于当前库。验证身份只由实际模型、凭据、
  地址和请求选项决定，来源标签用于解释继承关系，不使相同连接失效。
  旧版验证记录在实际连接一致时迁移复用；真实模型、密钥或地址变化仍要求重新测试。
- 已验证连接可立即排队；设置保存会使界面缓存失效，实际执行在取得知识库锁后再次检查
  所捕获配置的验证记录，避免绕过真实连接检查。成功后只提交一次原问题。取消、切换知识库或关闭原界面，
  不会让过期测试结果触发原操作。普通设置中的测试成功也会刷新概览状态。
- 向导共用应用的浅色／深色主题，使用 8px 间距倍数、单一主按钮和明确步骤标题。
  输入及操作支持键盘焦点，主要点击区域至少 44 个逻辑像素；正文可滚动，底部操作保持可见。

本轮发现与处理范围：

| 优先级 | 现象及影响 | 处理情况 |
| --- | --- | --- |
| P0 | 相同连接从全局继承变成本库显式值后，被当成未验证，发送再次弹配置 | 已修复验证身份；增加旧记录迁移和实际发送回归 |
| P0 | 引导测试全局连接，问答却使用本库覆盖，用户无法理解为何测试成功仍不能继续 | 已改为验证当前目标库；新建前使用全局默认 |
| P1 | 完整设置表单、来源下拉和多种保存按钮混在首次引导中，认知负荷过高 | 已拆为独立步骤；高级设置仍放在日常设置中 |
| P1 | “测试并继续”在日常设置中没有继续目标；概览始终显示“继续配置 / 原操作” | 已明确保存／测试文案；概览按待续操作显示按钮，连接就绪后隐藏配置入口 |
| P1 | 新库创建后下一步不明确 | 已在确认页说明后续动作，并在创建成功后进入资料页 |
| P1 | 资料搜索立即重绘结果且清空勾选；知识搜索逐键遍历全部节点 | 待优化：输入防抖；筛选变化时立即禁用批量动作，再统一更新结果与选择，避免误操作。未声称已测出性能退化 |
| P2 | 资料列表首次加载主要依靠“正在读取”文字，空态和加载态的视觉区分不足 | 待优化：首次加载使用骨架占位；刷新已有列表时保留内容和位置 |
| P2 | 全局普通控件热区尚未全部达到 44px；高级设置仍含 JSON 和来源术语 | 待优化：统一全局尺寸，补充结构化高级表单；本次只完成向导的热区和文案 |

原生 Qt 验收使用隔离的设置和知识库，以及本机回环 HTTP 服务；连接测试执行真实 SDK
文本与工具调用，问答验收到生产 `Conversations.send` 的请求提交边界。验证包含模型列表获取、
失败重试、返回保留输入、确认前取消不建库、继承复用、相同值的本库覆盖、密钥变化后的重新验证、
原问题只提交一次、浅色／深色和 480／720px 窗口，并以 100% 和 125% 缩放运行。
这属于源码级 Qt 验收，没有调用付费模型，也没有重新构建或验收 Windows 安装包。

```bash
.venv/bin/pytest -q tests/test_connection_reuse.py tests/test_model_catalog.py \
  tests/test_model_connections.py tests/test_onboarding.py \
  tests/test_desktop_config_inheritance.py tests/test_setup_wizard.py
.venv/bin/python scripts/verify_setup_wizard.py --headless --output /tmp/ultrakb-setup
QT_SCALE_FACTOR=1.25 .venv/bin/python scripts/verify_setup_wizard.py \
  --headless --output /tmp/ultrakb-setup-125
```

## Reproduce source acceptance

Use the existing pinned development and desktop dependencies plus the prepared
local rendering assets, as described in the desktop build guide. All commands
below create dedicated fixture KBs and profiles; output directories must be new.
No paid model endpoint or real credentials are used.

```bash
.venv/bin/pytest -q tests/test_desktop_workbench.py tests/test_desktop_lifecycle.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m ultrakb.desktop.verification \
  --workbench --output /tmp/ultrakb-workbench
QT_QPA_PLATFORM=offscreen .venv/bin/python -m ultrakb.desktop.verification \
  --workbench-restart /tmp/ultrakb-workbench --output /tmp/ultrakb-restart
QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=1.5 .venv/bin/python \
  -m ultrakb.desktop.verification --workbench --output /tmp/ultrakb-hidpi
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/verify_desktop_model.py \
  --urls --output /tmp/ultrakb-business
QT_QPA_PLATFORM=offscreen .venv/bin/python -m ultrakb.desktop.verification \
  --corpus tests/fixtures/native-render-corpus.json --output /tmp/ultrakb-corpus
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy ultrakb
.venv/bin/pytest
```

The workbench runner also checks the supplied fonts, intermediate fade frames,
reversed transitions, drawer resizing/focus, draft retention and IME-safe submission.
It drives the real Qt window, checks visible pages and keyboard
controls, verifies a saved draft and failure feedback through real tasks, switches
between same-named KBs, simulates palette input at the Qt boundary, and checks
preferences in a fresh process. Its screenshot matrix contains all seven pages
in both themes at 1366×768, 1920×1080 and 900×650 logical pixels. The same matrix
runs at 150% scaling. The existing business runner now exercises document,
conversation, artifact and ordinary settings management through the embedded
pages; restricted recovery and destructive confirmations remain modal.

## Actual application screenshots

Captured on 2026-09-08 with PySide6/Qt 6.11.2, Linux x86_64, the offscreen Qt
platform and 100% scale. These are actual Workbench captures from isolated sample
KBs, including empty source/artifact lists and a deliberately failed revision
check. The conversation screenshot uses a presentation fixture, not an actual model response.
Blank lists are empty states. Files are retained
without image editing; screenshot pixels include Qt's device scaling.

| Page | Screenshot |
| --- | --- |
| Overview, light, 1366×768 logical | [Overview](desktop-evidence/workbench/overview.png) |
| Documents, light, 1366×768 logical | [Documents](desktop-evidence/workbench/documents.png) |
| Knowledge, light, 1366×768 logical | [Knowledge](desktop-evidence/workbench/knowledge.png) |
| Conversations, light, 1366×768 logical | [Conversations](desktop-evidence/workbench/conversations.png) |
| Artifacts, light, 1366×768 logical | [Artifacts](desktop-evidence/workbench/artifacts.png) |
| Tasks, light, 1366×768 logical | [Tasks](desktop-evidence/workbench/tasks.png) |
| Settings, light, 1366×768 logical | [Settings](desktop-evidence/workbench/settings.png) |

This is source-run Qt behavior and visual evidence. Offscreen tray-menu activation
checks application behavior, not physical OS tray integration. It does not claim
fresh-machine Windows/Debian portable-distribution acceptance or a new release.
The accepted rendering engine and grammar are unchanged; the native corpus
scenario continues to cover Chinese, inline/display formula structure, Mermaid,
tables and code through the actual reader.


## Recorded checks and review

Source acceptance on 2026-09-08 (Python 3.12.13, Qt 6.11.2):

| Check | Result |
| --- | --- |
| Full pytest suite after review fixes | 1,474 passed, 60.06 seconds |
| Controlled local HTTP/model acceptance | 22 checks passed, including management through pages |
| Actual Qt corpus | 296 cases across both themes at 100/150/200/400% |
| Expected unsupported-input fallbacks | 16 cases, verified as explicit source fallbacks |
| Inline formula baseline checks | Maximum measured error 0.605 logical pixels |
| Screenshot matrix | 42 views at 100%; repeated at 150% device scale |
| Ruff / formatting / mypy | Passed |

Pytest reported a compiler-coroutine warning in `test_remove.py` and a coroutine
warning during final garbage collection; all tests completed successfully.
The corpus runner leaves its exhaustive visual verdict separate from structural
checks. This change includes representative screenshot inspection, not a renewed
qualification of every supported diagram at every scale.

The independent Standards review reported no findings. The Spec review caught a
Chinese fallback to a system font inside code; both the source editor and Markdown
code now name Source Han Sans after Source Code Pro. Actual glyph runs verify both
families and the weight axis. Follow-up review reported **Standards: 0 remaining;
Spec: 0 remaining**.


The font-weight follow-up reproduces the earlier 400/500 glyph-outline collapse
on Windows and Linux. Its native verifier additionally checks the real face name
and OS/2 weight after QSS inheritance and actual Markdown bold/italic rendering.
The original screenshot/check table above records the preceding redesign; the
matching build's delivery report contains follow-up evidence and package hashes.

## Continuous chat and compact layouts

The desktop composer uses automatic multi-turn conversations with no mode or save
checkbox. The history drawer opens conversations with a single click, while export
and deletion live in its More menu. In-progress work shows a status, not the raw
model trace. Completed answers use the SDK terminal output; explicit reasoning
envelopes are filtered without removing literal Markdown code examples.

Ordinary buttons have visible borders, sidebar labels use 18 px text, and action
rows wrap as space narrows. The layout capture matrix now includes 720 × 600
logical pixels. Native chat acceptance covers saved multi-turn history, tagged
reasoning exclusion, duplicate Enter prevention and a background result arriving
after the user starts a different conversation.


## 导航、导入进度与任务批量操作复核（2026-10-15）

本轮修复知识库写锁占用时的静默等待：知识标题、原文阅读和关注资料提问立即显示
读取或等待状态；切换工作区后撤销过期跳转。取消读取恢复旧正文和目录选中项；
同一标题重读失败后可以直接再次点击重试。读写一致性仍由原有知识库锁保护。

顶部说明采用实际内容尺寸分配宽度；导入资料与导入选项共用可换行操作区。
资料与任务行以操作控件的最小尺寸分配行高，验收断言完整可见区域和窗口坐标命中，
不只检查 isVisible 或直接向按钮中心发送事件。

导入区显示已处理/总数、当前文件和中文阶段，任务来源在排队和等待锁时也保留。
确认单项结果后请求刷新资料列表；列表等待写入完成时保留旧行并说明原因。
任务详情每 250ms 更新，保留阅读位置。运行中的阶段优先显示当前操作，历史失败
另保留在逐项结果中，不覆盖下一份资料的实时状态；不推测单份资料的编译百分比。

任务页增加搜索、状态/知识库范围筛选、独立勾选和全选当前结果。焦点与批量范围分离，
刷新/排序保持任务身份，筛选变化清空勾选。行操作仅作用于该任务；批量停止、重试和
清理先核对适用集合并显示跳过原因。重试共用原有凭据/版本校验，跨库操作绑定明确
选中的原任务知识库；清理仍保护目录监听引用并保留知识库文件和产物。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/test_gui_delivery.py \
  tests/test_gui_task_progress.py tests/test_desktop_tasks.py tests/test_task_actions.py
QT_SCALE_FACTOR=1.5 QT_QPA_PLATFORM=offscreen .venv/bin/pytest -q tests/test_gui_delivery.py
QT_QPA_PLATFORM=xcb .venv/bin/python tests/gui_delivery_probe.py documents --output /tmp/gui-documents
```

上述原生交互脚本使用隔离设置和知识库；进度与混合任务批量界面使用明确标注的任务状态/
调度边界注入，既有真实零模型 worker 用例验证锁等待、只重试失败单元和历史清理。
它们不声称执行了真实模型导入，也不代替发行包或其他桌面平台验收。
