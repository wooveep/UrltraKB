# 可丢弃的原生内容渲染原型

回答的问题：[原型：公式与九类 Mermaid 能否在最终 Qt 控件中正确显示？](https://github.com/wooveep/UrltraKB/issues/6)。这是独立的 Qt Widgets 样本窗口，不是正式桌面应用，也不是免安装发布包。它不导入 OpenKB 业务模块，不打开或修改知识库。

当前结论与限制见 [REPORT.md](REPORT.md)。**技术生成成功、人工画面核对、用户确认、Windows 实测是不同证据，不能相互替代。**

## 启动

本机已准备好运行环境，在仓库根目录运行：

```sh
python3 openkb/desktop_prototype/launch.py
```

首次在其他开发机准备时，需要 Python、uv、Rust/Cargo 1.95.0；Windows 还需要 Rust MSVC 工具链所需的构建工具。随后同一条命令会在原型目录内准备 Python 3.12.13、依赖、Node、字体及原生 helper。Windows 命令为 `python openkb/desktop_prototype/launch.py`。Windows 启动流程已编写，尚未在 Windows 11 执行验证。

首次准备需要网络。资源准备完成后，正常运行使用本地文件。用 `--prepare` 重新执行校验和构建。不要把此开发流程当成普通用户的便携包验收。

窗口左侧选择样本；中间对照 Qt 直接绘制 SVG 与 resvg 生成 PNG 后由 Qt 绘制的结果。**重点验收 PNG 通道**；SVG 通道保留用于展示真实差异。切换主题和缩放会重新渲染 PNG；适应窗口最多放大至两倍，按设备像素比准备像素。下方可编辑、复制原文。公式正文页使用 PNG 和 MathJax 声明的深度进行基线放置。

```sh
# 指定样本与主题；截图来自真实 QWidget.grab()
openkb/desktop_prototype/.venv/bin/python openkb/desktop_prototype/run.py \
  --sample math-inline --paragraph --capture /tmp/openkb-inline.png
```

## 独立复验

以下命令在原型目录执行。Windows 将 `.venv/bin/python` 替换为 `.venv\Scripts\python.exe`。

```sh
.venv/bin/python check_corpus.py
.venv/bin/python capture_corpus.py artifacts/batch-both-2.json
```

Debian 可使用无外部网卡的网络命名空间运行完整语料，验证资源不依赖在线获取：

```sh
unshare --user --map-root-user --net .venv/bin/python check_corpus.py
```

脚本运行 32 个样本 × 明暗主题 × 100% / 150% / 200% / 400%，记录技术成功、预期错误、原文保存与运行事实。它**不自动批准图表语义或用户体验**。具体人工核对项直接写在 [samples.py](samples.py) 中。

## 依赖与资源

| 部件 | 精确版本 / 来源 | 用途 |
| --- | --- | --- |
| Python | 3.12.13 | 独立窗口与进程编排 |
| PySide6-Essentials / shiboken6 | 6.11.2 / 6.11.2 | Qt Widgets、QtSvg、QtGui；未安装含 WebEngine 的 Addons |
| Node | 24.20.0，Node 官方 x64 压缩包 | 只在本地运行 MathJax；Windows ZIP 与 Linux tar.xz 的 SHA-256 在 bootstrap.py 锁定 |
| @mathjax/src / @mathjax/mathjax-newcm-font | 4.1.3 / 4.1.3 | TeX 排版、数学字形路径；npm 完整锁见 package-lock.json |
| merman / merman-render | 0.7.0 / 0.7.0 | Mermaid 解析、布局及 resvg-safe SVG |
| resvg | 0.46.0 | 显式字体数据库；启用 text、raster-images，不启用 system-fonts |
| rustybuzz | 0.20.1 | 使用同一批 Noto 字体为 Merman 实测文字宽度 |
| Noto Sans CJK SC | Sans2.004，Regular / Bold | Qt 注册两字体；Rust 布局与 resvg 分别显式读入相同字体文件 |

Node 中 MathJax 使用随 npm 包提供的数学字形。中文是 SVG text，最终由 resvg 的 Noto 字体库绘制；不能声称 Qt 注册字体就使 Node 自动读取了字体。MathJax LiteDOM 对未知文字的度量仍有近似，当前中文样例的画面核对不保证任意混排文本都正确。

[resource-manifest.json](resource-manifest.json) 记录下载来源与 SHA-256；[Cargo.lock](rust-helper/Cargo.lock) 锁定原生传递依赖。字体文件、Node 自带许可文本及 npm 包许可文本保留在本地资源中。分发许可和完整产品依赖包另行决策，本原型不替代该工作。

## 文件

- `bootstrap.py` / `launch.py`：准备本地资源与启动。
- `mathjax_render.mjs`：一个 TeX 请求变成一个完整 SVG，明确检测错误。
- `rust-helper/`：Merman、真实字体测量、显式字体库及 PNG 生成。
- `svg_adapter.py`：记录在报告中的有限输出适配；不更改 Markdown。
- `render.py`：独立任务进程，保存可丢弃产物和诊断。
- `samples.py`：18 张 Mermaid、12 个公式、2 个明确错误。
- `run.py` / `capture_corpus.py`：最终 Qt 显示、正文基线与截图。
- `evidence/`：选定的运行事实、窗口截图及检查记录。

`.venv/`、`.runtime/`、`node_modules/`、Rust `target/`、`artifacts/` 全部忽略，不提交大体积运行环境。它们仅位于原型目录内。调试协议和原型进度使用标准输出，与产品日志设施无关。
