# 无浏览器渲染：进入原型的版本、输出通道与资源证据

核查日期：2026-09-07。对应决策票：[研究：无浏览器渲染候选应使用哪些版本、输出通道与随包资源？](https://github.com/wooveep/UrltraKB/issues/3)。边界来自已确认的 [Spec: Native desktop workbench with CLI/REST compatibility and portable Windows/Linux releases](https://github.com/wooveep/UrltraKB/issues/1)：Qt Widgets、禁止任何浏览器引擎、本地离线内容渲染、Windows 11／Debian 13.6 x86_64、公式与九类 Mermaid 语义正确。

**研究结论：候选足以进入原型，尚不足以锁定生产依赖。** 优先验证 MathJax 4.1.3 + Node 24.20.0；Merman 0.7.0 同时验证安全 SVG→Qt 与可控字体的 PNG→Qt。最关键的新增缺口是：Merman 现成 PNG 通道读取系统字体，不能据此承诺便携字体闭包；MathJax 默认 TeX 组件会把未知宏画成红色文字，不能把“有 SVG”当作成功。

本轮阅读官方文档、版本化源码和发行清单；仅下载 MathJax 字体 npm 包，核对内容与完整性。**未安装或运行渲染依赖，未构建原型、未进行视觉验收、未验证目标系统。** 下文将源码事实与原型建议分开。

## 1. 候选版本与获取地址

| 组件 | 进入原型的固定候选 | 已核实的来源与界限 |
| --- | --- | --- |
| 公式引擎 | `@mathjax/src=4.1.3` | [该版本源码清单][mj-package]、[发布记录][mj-release]；对应 npm [元数据][mj-npm]提供完整性摘要及[源码包][mj-tar]。不要把 `@mathjax/src@4` 写进最终锁文件。 |
| 公式字形数据 | `@mathjax/mathjax-newcm-font=4.1.3` | 引擎直接精确依赖该版；[字体 npm 元数据][mj-font-npm]与[字体包][mj-font-tar]均可获取。本轮下载包的 SHA-512 与元数据一致，见下文资源说明。 |
| JS 运行时 | Node **24.20.0 LTS** | [官方版本目录][node-dist]有 `node-v24.20.0-win-x64.zip`、`node-v24.20.0-linux-x64.tar.xz` 与 `SHASUMS256.txt`／签名。Node 24 为 [LTS 系列][node-releases]。MathJax 4.1.3 的[源码 CI][mj-ci]使用 Node 24，这是选择依据；其 package.json **没有 `engines`**，本轮不能给出有官方承诺的完整最低／最高 Node 范围，也不能把 npm 发布者的 `_nodeVersion` 当作运行要求。 |
| Mermaid 引擎 | Merman **0.7.0** | [发行页][merman-release]提供 `merman-cli-x86_64-pc-windows-msvc.zip`、`merman-cli-x86_64-unknown-linux-gnu.tar.xz` 及各自 `.sha256`。以相同版本号获取，不能将主分支能力归入此版本。 |
| 原生 SVG 栅格化 | Merman 的 PNG 通道；需要字体控制时，原型比较使用其库的独立 Rust helper | [工作区清单][merman-cargo]直接使用 `usvg/resvg=0.46.0`，`svg2pdf=0.13.0`；[锁文件][merman-lock]同时存在传递的 `usvg/resvg=0.45.1`，不能粗略写成整个发行物只有一种版本。若自行构建，Merman 声明 Rust 最低版本为 **1.95**；用户机器无需 Rust。 |
| 中文字体 | Noto Sans CJK SC **2.004** 作为首批测试字体 | [发布页][noto-release]提供 `08_NotoSansCJKsc.zip` 等；建议从该固定版选择未修改的常规／粗体 OTF，记录实际文件名和摘要，验证通过后才纳入程序资源。无需安装到系统字体目录。 |
| 显示端 | 与便携运行研究／原型统一的精确 PySide6 版本 | 本报告核查 Qt 6.11.2 文档能力，不独立决定 PySide6 发行版本。最终报告必须记下实际 PySide6、Qt、插件版本，不能仅写“Qt 6”。 |

Node 24.20.0 的[构建支持表][node-building]列 Linux x64 的 kernel≥4.18、glibc≥2.28，以及 Windows x64≥Windows 10；这是 Node 自身的条件，不替代 Merman／Qt／整个便携包在目标系统的运行检查。

## 2. MathJax：离线公式 → 独立 SVG

**有官方依据的调用方式：** 使用本地组件 loader，设置 `paths.mathjax='@mathjax/src/bundle'`、本地 `require`／`import`，加载 `input/tex`、`output/svg`、`adaptors/liteDOM`，设置 `output.font='mathjax-newcm'`，等待 startup 后调用 `tex2svgPromise(tex, {display, em, ex, containerWidth})`。LiteDOM 是 MathJax 的轻量 DOM 适配器；这条调用不需要浏览器、WebView 或 HTTP 服务。[Node 组件文档][mj-node]

**原型建议：** 使用组件的最小加载方式，避免为了公式图片加载菜单、浏览器交互或语音组件。进程结束时调用 `MathJax.done()`；若启用了语音组件，其 worker threads 会影响 Node 正常退出。运行时只保留本地加载路径，完整携带所启用的扩展和动态字形块，不能在渲染时从 CDN 补齐缺项。[Node 组件文档][mj-node]

**独立 SVG 条件：** `svg.fontCache` 取 `local` 或 `none`；`global` 会引用文件外的共享字形缓存。把所需 CSS 带入 SVG，明确前景色，并序列化真正的 `<svg>` 根节点。SVG 的尺寸、行内公式深度／基线仍须交给 Qt 排版；浏览器样式中的 `vertical-align` 不会自动成为 QTextDocument 的行内基线。[独立 SVG 官方示例][mj-standalone] 后一句是显示集成推论，须在控件中验证。

**资源闭包不是“复制一个 tex-svg.js”：** MathJax v4 可能在渲染过程中按需读取字形数据。字体组件、`svg/` 动态范围与所选模块路径必须完整可用；使用原始 npm 布局的原型先保留完整包，待离线语料通过后再裁剪。`@mathjax/src` 还声明 `mhchemparser:^4.2.1`、`mj-context-menu:^1.0.0`、`speech-rule-engine:5.0.0-rc.4`；前两项是上游范围，不是本项目获准使用浮动依赖。原型产物须产生精确依赖与文件摘要清单；若裁掉不使用的组件，必须证明没有动态加载回到这些依赖。[版本清单][mj-package]、[字体加载文档][mj-fonts]

**输入与失败判定：**

- 文档分词层保留已经确认的 `\(…\)`／`\[…\]` 与小写 `mermaid` fence；向 `tex2svgPromise` 传去掉定界符的 TeX 和显式 `display`，不顺手扩大 `$…$` 语法。这是规格约束；MathJax 提供[可配置的定界符][mj-tex-options]，不能让其默认值替代产品语法。
- `input/tex` 4.1.3 预加载 `base/ams/newcommand/textmacros/noundefined/require/autoload/configmacros`。[版本化组件源码][mj-tex-component] `noundefined` 会生成未知控制序列的红色文本，**不会抛出错误**。[版本化错误处理源码][mj-noundefined] 原型要关闭该替代行为或显式捕获并报告诊断，同时检查 `merror`，不能只检测进程退出码和输出非空。
- 常用分数、根式、上下标、积分、矩阵、分段、多行对齐及语料所需宏逐项与[官方宏表][mj-macros]对应。KaTeX 宏和 MathJax 宏不能仅按同名推定等价；`\text{}` 中额外宏依赖 `textmacros`。自定义宏和自动扩展只按固定语料启用。
- MathJax 字库找不到的字符会依赖替代字体；在 Node 中连字符宽度也无法实际测量，可能影响布局。中文 `\text{中文说明}`、混合标点、罕见字符必须验证位置和遮挡。**后续换成 PNG 或换字体不能自动修复前一阶段错误的宽度。**[字体回退文档][mj-fonts]

## 3. Merman：九类范围与三种输出不可混淆

Merman 0.7.0 的基线是 **Mermaid 11.15.0**。[版本 README][merman-readme] 规格记录旧 Web 为 Mermaid 11.16.0，因此存在明确的上游版本差；本轮不声称全语法等价。它的[版本化矩阵][merman-status]对 ER、流程、状态、类、时序、饼图、Gantt、C4、思维导图九类均列出解析、布局、SVG 渲染、上游 SVG 基线和比较工具。那些是项目自有语料的证据，未证明本项目的最终 Qt 图片效果。

| 通道 | 0.7.0 的事实 | 原型用途 |
| --- | --- | --- |
| 原始 SVG | `merman-cli render input.mmd --format svg --out output.svg` 默认走 `SvgPipeline::parity()`；可保留 `foreignObject`。CLI 参数定义中未提供选择 `resvg_safe` SVG 预设的开关。[CLI 参数][merman-cli]、[CLI 实现][merman-cli-render] | 仅作诊断／结构对照，不能当成可直接给 Qt 的最终产物。 |
| resvg-safe SVG | Rust API `HeadlessRenderer::render_svg_resvg_safe_sync()` 或 `SvgPipeline::resvg_safe()` 将常见 HTML 标签换成 SVG 文本，移除原 `foreignObject`，清理若干 CSS／非有限值问题。[README][merman-readme]、[栅格文档][merman-raster-doc] | 通过一个仅负责转换的原型 helper 取得，交给 Qt 的真实控件；仍须测 CSS、裁剪、文本和箭头。 |
| PNG | `merman-cli render input.mmd --format png --out output.png --scale 2 --background transparent` 内部应用 safe 管线，再用 usvg/resvg 栅格化。[CLI 实现][merman-cli-render] | 可直接验证现成发布二进制，但字体闭包见下一节。比较不同缩放和高 DPI 下的文字、连线清晰度。 |

PNG 大小来自 viewBox，先取整再应用 scale。CLI 有 `--raster-fit-width/height` 与最大宽／高／像素限制，默认边长上限 8192；建议保留限制，用视口尺寸和 device pixel ratio 请求重渲染，记录是否被限幅，而不是无限放大一次位图。[栅格说明][merman-raster-doc]、[CLI 参数][merman-cli]

复杂嵌套 HTML、图标、富样式标签在 safe／PNG 路径可能退化；上游明确称其近似处理。[栅格说明][merman-raster-doc] 因此普通文本也要检查“文字仍在且属于正确节点”；特殊 HTML／图标／图内公式仍按已接受的边界样本记录，不能扩大成任意兼容承诺。

**两个易漏行为：** `--iconPacks` 本地找不到包时会尝试从 unpkg 下载；离线路线只能使用明确的本地图标 JSON，缺失时报错。默认 `--math-renderer` 是 `none`；即便二进制默认编入 RaTeX，也须显式 `--math-renderer ratex` 才启用，该能力不等于图内任意公式通过。不要传 `--suppress-errors` 把错误图当作成功。[CLI 参数][merman-cli]、[图标获取实现][merman-cli-render]、[图内数学说明][merman-readme]

同一个 CLI 能将以 `<svg` 开始的原始 SVG 输入转换成 PNG，因此可列为 MathJax SVG 的对照通道；源码检测是 `trim_start().starts_with("<svg")`，带 XML 声明的官方独立 SVG 示例须先规范化为 SVG 根节点。该行为来自[版本化实现][merman-cli-render]，本轮没有实际运行，不能承诺所有 MathJax SVG 都能被这一通道保真转换。

## 4. 字体闭包与最终 Qt 控件：必须在原型作出的决定

Merman 的 `RasterOptions` 没有字体路径／fontdb 字段，现成 CLI 也没有字体目录选项；PNG 实现调用 `fontdb.load_system_fonts()` 后选取可用字体。[栅格源码][merman-raster-src] **将 OTF 放在程序目录里，不等于 Merman 已使用它；Qt 注册字体也不会自动改变另一进程的 fontdb。** 后者是依据各进程 API 边界作出的集成推论。

原型比较以下两个有来源支持的组合，按最终效果选择，不先规定产品内部绑定方式：

1. **安全 SVG→Qt：** Merman 库 helper 输出 safe SVG；Qt 通过 `QFontDatabase.addApplicationFont` 注册随包 OTF／TTF／TTC，再由 `QSvgRenderer`／`QSvgWidget` 显示。Qt 官方明确支持这些字体格式和应用内加载，但字体注册成功不证明图表文字度量正确。[Qt 字体 API][qt-font]、[QSvgWidget][qt-svg-widget]
2. **可控字体的 Rust 栅格→PNG→Qt：** helper 使用 Merman 的 safe SVG，再向 usvg 0.46.0 的 `Options.fontdb` 显式提供随包字体数据库后栅格化。usvg 有这一接口；它不是现成 Merman CLI 已暴露的选项。[usvg Options 源码][usvg-options] 是否需要此 helper、对中文布局有无改善，必须运行证明。

Qt SVG 支持 SVG 1.2 Tiny 静态子集及扩展，官方扩展说明仍将 `clipPath` 列为不支持；因此不能从“resvg-safe”推导“Qt-safe”。[Qt SVG 特性][qt-svg-features] 对 MathJax 的 CSS／引用／遮罩，对 Merman 的箭头／裁剪／换行，均验收到最终控件而非停在 SVG 文件存在。

行内公式可比较自定义 QTextDocument 对象绘制与图片资源；`QTextObjectInterface` 允许报告尺寸并绘制对象，但文档还指出自定义对象在剪贴板复制中会被忽略。[Qt 文本对象接口][qt-text-object] 原型必须独立提供、验证公式／图表源码复制，不能凭画面正确就假定正文复制、基线和编辑联动已完整。

## 5. 随包资源与来源告知

| 资源 | 资料核查结果／原型要求 |
| --- | --- |
| Node | 保留对应平台二进制、发行版本和校验摘要，携带其完整 [LICENSE][node-license]（其中有第三方组件条款），不要只放一条 MIT 标签。npm 工具和头文件不是执行已准备好组件的必要前提，但裁剪后的离线启动要测。 |
| MathJax | 引擎为 Apache-2.0；随包保留 [LICENSE][mj-license]、著作权／来源和所选扩展实际依赖的通知材料，改动须记录。 |
| MathJax 字形数据 | npm 4.1.3 的 `license` 字段为 Apache-2.0。本轮实际核验的字体 tarball 没有文件名含 LICENSE／NOTICE／COPYING 的条目，其声明的 `MathJax-fonts` GitHub 仓库及 gitHead API 返回 404；**元数据可核实，完整字体来源／通知材料尚未收齐**，不得写成“再分发材料已齐备”。原型报告应保留该缺口，发行前补齐，必要时采用通知材料可追溯的固定字库。 |
| Merman | 本体为 `MIT OR Apache-2.0`，发行材料保留许可证及 [THIRD_PARTY_NOTICES][merman-notices]，特别是 Mermaid 来源归属；实际捆绑的 Rust 依赖另按锁文件生成材料，不能用本体许可替代所有依赖。 |
| 中文字体 | Noto Sans CJK 2.004 的 [OFL 1.1 许可][noto-license]允许随软件分发，要求保留许可及著作权，不将字体独立售卖；若修改／子集化需检查其名称条件。原型优先使用原版 OTF，记录精确文件及校验摘要。 |
| 图标 | 首批普通语料不需要图标包；若用户在原型确认图标样本，则新增本地文件清单及对应图标包许可，禁止默认联网补全。 |

已下载字体包完整性：`sha512-gzAB3dFHilHX1l5x2xUqRL+1jDQt3Fyza1DkEMVXWC4E8SvsGdlgEza47HYi2WhVcgfkvf4zgUGzuhbq3Pjlew==`。[npm 元数据与包地址][mj-font-npm] 这是文件一致性检查，不是渲染或再分发验证。整个便携包的 Qt／Python 许可清单由便携运行研究承接，本报告只交付渲染资源部分。

## 6. 交给原型的最小验证矩阵

以下为验证建议，**各行均尚未执行**。延续用户已确认的“最小合成样本起步，原型时本人确认”方式；不以源码回退冒充支持范围通过。

| 语料／场景 | 必须取得的证据 |
| --- | --- |
| 公式基础 | 行内／块级各覆盖分数、根式、上下标、积分、矩阵、分段、多行对齐；保存原文、MathJax SVG、Qt 截图，检查结构、基线、截断。 |
| 公式字体／宏 | 中文 `\text{}`、中英混排、罕见符号、常用扩展、自定义宏；未知宏与语法错误明确失败；记录字形块、字体、尺寸及扩展配置。 |
| 九类 Mermaid | 每类至少一份最小图及一份带中文／长标签／换行的图。分别检查节点文字、边与方向、时序与状态关系、ER 基数、饼图数值、Gantt 日期、C4 边界。对时间相关样本固定 `--fixed-today` 和时区偏移，避免每日变化。 |
| 三条显示链 | 同一语料保存原始 SVG、安全 SVG、最终 PNG（若采用），并记录 Qt 控件中的结果；不能仅使用上游 DOM 比较。测缩放 100%／150%／200%／400%、浅深主题与多幅图引用不串用。 |
| 真正离线／便携字体 | 断网、空缓存、无预装 Node／Rust、可控的系统字体集合；证实实际加载的是随包字体。Windows 11 与 Debian 13.6 GNOME/X11 分别记录运行结果。 |
| 文档集成 | 多公式与图表混合正文、窗口宽度变化、滚动、编辑后重渲染、源码复制；源文档不被转换器改写。 |
| 边界内容与失败 | 复杂 HTML、图标、图内公式分组记录，用户明确接受的样本才进入支持集；测试缺字体、缺扩展、超大图、无效语法，保持可见错误而非静默漏字。 |

原型必须回答的明确问题是：**哪条最终显示链在两目标系统及随包字体条件下通过已确认语料，并能可靠区分渲染错误与成功？** 若中文度量或安全输出转换仍破坏语义，路线尚未成立；不能以放宽已确认范围、恢复浏览器引擎或隐藏失败替代决策。性能、缓存裁剪和具体 helper 形态在拿到这些证据后再定。

[mj-package]: https://github.com/mathjax/MathJax-src/blob/4.1.3/package.json
[mj-release]: https://github.com/mathjax/MathJax-src/releases/tag/4.1.3
[mj-npm]: https://registry.npmjs.org/@mathjax/src/4.1.3
[mj-tar]: https://registry.npmjs.org/@mathjax/src/-/src-4.1.3.tgz
[mj-font-npm]: https://registry.npmjs.org/@mathjax/mathjax-newcm-font/4.1.3
[mj-font-tar]: https://registry.npmjs.org/@mathjax/mathjax-newcm-font/-/mathjax-newcm-font-4.1.3.tgz
[mj-ci]: https://github.com/mathjax/MathJax-src/blob/4.1.3/.github/workflows/test.yml
[mj-node]: https://docs.mathjax.org/en/latest/server/components.html
[mj-standalone]: https://docs.mathjax.org/en/latest/web/convert.html#creating-stand-alone-svg-images
[mj-fonts]: https://docs.mathjax.org/en/latest/output/fonts.html
[mj-tex-options]: https://docs.mathjax.org/en/latest/options/input/tex.html
[mj-tex-component]: https://github.com/mathjax/MathJax-src/blob/4.1.3/components/mjs/input/tex/tex.js
[mj-noundefined]: https://github.com/mathjax/MathJax-src/blob/4.1.3/ts/input/tex/noundefined/NoUndefinedConfiguration.ts
[mj-macros]: https://docs.mathjax.org/en/latest/input/tex/macros/index.html
[mj-license]: https://github.com/mathjax/MathJax-src/blob/4.1.3/LICENSE
[node-dist]: https://nodejs.org/download/release/v24.20.0/
[node-releases]: https://nodejs.org/en/about/previous-releases
[node-building]: https://github.com/nodejs/node/blob/v24.20.0/BUILDING.md
[node-license]: https://github.com/nodejs/node/blob/v24.20.0/LICENSE
[merman-release]: https://github.com/Latias94/merman/releases/tag/v0.7.0
[merman-readme]: https://github.com/Latias94/merman/blob/v0.7.0/README.md
[merman-cargo]: https://github.com/Latias94/merman/blob/v0.7.0/Cargo.toml
[merman-lock]: https://github.com/Latias94/merman/blob/v0.7.0/Cargo.lock
[merman-status]: https://github.com/Latias94/merman/blob/v0.7.0/docs/alignment/STATUS.md
[merman-cli]: https://github.com/Latias94/merman/blob/v0.7.0/crates/merman-cli/src/cli.rs
[merman-cli-render]: https://github.com/Latias94/merman/blob/v0.7.0/crates/merman-cli/src/render.rs
[merman-raster-doc]: https://github.com/Latias94/merman/blob/v0.7.0/docs/rendering/RASTER_OUTPUT.md
[merman-raster-src]: https://github.com/Latias94/merman/blob/v0.7.0/crates/merman/src/render/raster.rs
[merman-notices]: https://github.com/Latias94/merman/blob/v0.7.0/THIRD_PARTY_NOTICES.md
[usvg-options]: https://github.com/linebender/resvg/blob/v0.46.0/crates/usvg/src/parser/options.rs
[noto-release]: https://github.com/notofonts/noto-cjk/releases/tag/Sans2.004
[noto-license]: https://github.com/notofonts/noto-cjk/blob/Sans2.004/LICENSE
[qt-font]: https://doc.qt.io/qt-6/qfontdatabase.html#addApplicationFont
[qt-svg-widget]: https://doc.qt.io/qt-6/qsvgwidget.html
[qt-svg-features]: https://doc.qt.io/qt-6/svgextensions.html
[qt-text-object]: https://doc.qt.io/qt-6/qtextobjectinterface.html
