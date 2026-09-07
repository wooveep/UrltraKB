# 原生渲染原型：Debian 证据与待补项

2026-09-07。对应 [原型：公式与九类 Mermaid 能否在最终 Qt 控件中正确显示？](https://github.com/wooveep/UrltraKB/issues/6)。

**Debian 上可继续采用“MathJax / Merman → 显式字体与输出适配 → resvg PNG → Qt”的候选路线。用户已在原生窗口反馈“右侧无异常”。Qt 直接显示 SVG 的统一路线不可采用。Windows 侧用户另已反馈“已验证无问题”，正在补所用版本与范围记录；目前不把这条反馈扩写为代理取得了 Windows 日志或截图。**

这是渲染原型结论，不是正式阅读器、打包成果或任意 Mermaid 语法的兼容承诺。

## 证据分层

| 证据 | 已得到的事实 | 实际边界 |
| --- | --- | --- |
| 真实 Qt 窗口 | Debian 13.6、x86_64、GNOME/X11、Qt 6.11.2、xcb；Widgets 窗口真实启动并截图 | 不是 offscreen；Windows 为用户报告通过，详细运行记录待补 |
| 完整技术复验 | 32 样本 × 2 主题 × 4 缩放 = 256 次；240 次正常生成，16 次明确预期错误；原文全部保持不变 | 这些计数不等于自动语义验收 |
| 断网 | 网络命名空间只有 lo；外连返回 Network is unreachable；全部技术复验完成，75.62 秒 | 资源预先下载；不证明完整应用或便携包离线运行 |
| 输出结构与字体 | 成功输出不含 foreignObject；resvg 每次显式加载两份 Noto 字体；Merman 测宽使用相同文件 | 不能泛化为所有字体、所有排版模式 |
| 用户观看反馈 | 对已打开窗口右侧 PNG，用户回复 **“右侧无异常”** | 未提供逐样本评分；本轮未补充额外语料 |

原始记录：[运行事实](evidence/runtime-facts.json)、[用户反馈](evidence/user-feedback.json)、[Qt 控件截图事实](evidence/corpus/qt-capture-facts.json)、[资源来源与哈希](resource-manifest.json)。机器记录中的 `pending` 是运行时未收集用户反馈的状态，最新人工反馈以上述独立记录为准。

## 原型范围

语法沿用项目 `\(…\)`、`\[…\]` 与小写 `mermaid` 围栏。具体语料和每个样本的核对要求见 [samples.py](samples.py)：

| 类别 | 合成样本与核对点 |
| --- | --- |
| 流程图 | 分支、回环、中文长节点；分支词与箭头方向 |
| 时序图 | 实线请求、虚线响应；三参与者、中文与 loop |
| 类图 | 继承空心三角；中文类名、属性方法、1/many 关联 |
| 状态图 | 起止点、回环；三个中文状态与转移 |
| ER | 一对零到多、PK/FK；中文属性说明 |
| 思维导图 | 三主分支与子层级；长中文完整 |
| 饼图 | 60/30/10 比例；数值、图例和扇区对应 |
| Gantt | 明确日期、after 依赖、里程碑；中文章节与任务 |
| C4 | Person、System、System_Ext 与 Rel；中文名称、说明、方向与 HTTPS |
| 公式 | 分数、根式、上下标、极限/求和/积分、矩阵、分段、多行对齐、中文/中文分数、重音/向量、伸缩括号、行内基线、局部宏 |
| 错误 | 未知宏和无法识别的图表；明确报错且保留原文 |

本轮人工窗口使用右侧 PNG；公式正文基线页也已改用 PNG，避免 SVG 嵌套元素丢失。截图包含 SVG 对照中的已知失败，不能把左侧错误当成已修复。

## 实测发现与必要适配

1. **中文节点超出边框。** Merman 内置测量回退没有使用随包 Noto 的真实宽度。通过公开 TextMeasurer 接口，用 rustybuzz 对相同 Regular/Bold 字体进行测量后，当前中文标签能放进节点。该最小适配支持显式换行；自动折行、复杂 HTML 标签尚未验证。
2. **分段公式的 `<` 使 SVG 无法解析。** MathJax LiteDOM 的 HTML 序列化保留了 data-latex 属性中的 `<`。导出前移除不参与显示的 TeX 元数据，原始 Markdown 单独保留；公式本体不更改。
3. **行内公式丢失后续片段。** MathJax 4 默认行内换行可产生多个 SVG；仅取 firstChild 会遗漏 `+x²`。关闭行内拆分，并断言容器恰好含一个完整 SVG；遇到其他结构明确报错。
4. **Qt SVG 丢失视觉内容。** 最终 Qt 对照实际出现类图/ER 标签黑底、思维导图样式和连接差异、时序样式差异，以及公式 `overline` 横线丢失。Qt 还报告跳过嵌套 SVG。故不批准原始 SVG 直接进入 Qt 的通用链路；PNG 保留完整公式与图表。
5. **类图继承符号变成实心。** resvg 未保留 marker 上继承来的透明填充。对 extension/aggregation 的实际子图形明确设置空心样式，恢复语义。没有改箭头方向或模型关系。
6. **C4 缺人物图标。** helper 起初未启用 resvg 的 raster-images，无法显示 Merman 自带的内联 PNG。启用该特性后恢复图标；仍无浏览器或系统字体特性。
7. **明暗配色与 C4 标签。** 采用 Merman 的 editor_light/editor_dark host theme，并为 C4 明确设置文字对比度、继承基线。Merman 0.7.0 的 C4 文本写入器把边中点再加半个标签宽度，导致标签压到目标节点；最小输出适配把标签置于既有直线/二次曲线位置并加背景。保留节点、端点、方向及全部原文。其他 C4 形态、用户自定义偏移和复杂样式不能据此宣称已兼容；需要新增语料或上游修复。
8. **PNG 缩放。** 适应窗口按最多 2× 及设备像素比准备；100/150/200/400% 重新生成对应像素。长图在适应窗口下字会变小，需要原尺寸/滚动检查。像素上限为单边 8192、总像素 33,554,432；超限明确报错，本轮语料未触发。

适配集中在 [SVG 适配](svg_adapter.py)、[字体测量](rust-helper/src/fonts.rs)、[MathJax 导出](mathjax_render.mjs)。这些是原型代码；正式实现需明确兼容策略并重写，不能直接复制成产品保证。

## 字体与运行链

Qt、Merman 布局、resvg 各自显式注册/读取 Noto Sans CJK SC Sans2.004 的 Regular、Bold；哈希与来源已记录。Qt 注册不是其他进程加载字体的证据，原生 helper 使用空 fontdb 再载入两份文件。

MathJax 使用 @mathjax/mathjax-newcm-font 4.1.3 的随包数学字形路径；中文输出为 text，最终通过 resvg 的 Noto 字体绘制。LiteDOM 对未知文字的度量仍含近似，当前中文样本通过观察不意味着任意复杂混排都可靠。

Python 只安装 PySide6-Essentials/shiboken6 6.11.2；Node 24.20.0 使用官方压缩包；Merman/merman-render 0.7.0、resvg 0.46.0、rustybuzz 0.20.1。依赖与下载锁在 README、npm lock、Cargo.lock 和 bootstrap.py 中。

## 仍需完成

- **Windows 11 x86_64 的记录**：用户已回复“已验证无问题”。已请求补充 Windows 版本、原型路径或版本、是否覆盖本批语料/主题/缩放，以对应到同一候选方案。当前没有代理获取的 Windows 日志或截图，不要求用户重跑已完成的验证。
- 用户若补充真实内容，继续纳入语料并修复支持范围内的问题；不能源码回退当成功。
- 完整 Markdown 阅读中的内链解析、本地图片权限/路径、编辑往返、资源相对路径、列表/表格混排、多个行内对象的换行与选择复制需单独集成验证。本原型只是单样本和单行基线探针。
- helper/font/主题/C4 适配的维护成本需要进入最终实施路线。许可与完整依赖便携运行沿用各自决策票，当前证据不替代它们。

## 截图入口

- [中文流程与两通道对照](evidence/corpus/flow-basic-light.png)
- [时序图明暗差异](evidence/corpus/sequence-basic-dark.png)
- [类图语义与标签](evidence/corpus/class-basic-light.png)
- [ER 中文与基数](evidence/corpus/er-basic-dark.png)
- [思维导图](evidence/corpus/mindmap-basic-dark.png)
- [饼图比例与图例](evidence/corpus/pie-basic-dark.png)
- [Gantt 日期与依赖](evidence/corpus/gantt-basic-light.png)
- [C4 适配后的 Qt 窗口](evidence/c4-cn-final-light-qt.png)
- [中文分数](evidence/corpus/math-chinese-light.png)
- [Qt SVG 横线丢失与 PNG 对照](evidence/math-accents-final-qt.png)
- [完整行内公式和正文基线](evidence/math-inline-baseline-qt.png)

截图来源为 QWidget.grab()，没有使用浏览器或生成式图像替代实测。
