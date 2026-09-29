# 随包 LibreOffice、UNO 与字体的可交付组合

研究日期：2026-09-29。对应[《研究：随包 LibreOffice、UNO 与字体的可交付组合是什么？》](https://github.com/wooveep/UrltraKB/issues/67)；本地代码基线为 `f4d7d1d34bbaf5330963accecf4f5304a9155aed`。

## 可交接结论与证据边界

建议以 **TDF LibreOffice 26.2.6 官方发行物、源码候选标签 `libreoffice-26.2.6.3`、同包 Python/UNO、应用现有字体** 作为第一轮交付验证组合。Windows x64 与 Debian x86_64 分别构建、分别验收；最终发行物选择仍属于后续交付契约。选择这个维护分支的具体补丁版本，是为了得到可锁定的上游物料和同版接口依据，并非因为“最新”。25.8.7 已是该系列最后一个维护版，TDF 明确其维护于 2026-06-12 结束；这一结论不等于 Debian 自行回补的旧版本也停止获得 Debian 修复。[官方发布目录](https://download.documentfoundation.org/libreoffice/stable/)、[25.8.7 维护公告](https://blog.documentfoundation.org/blog/2026/05/12/tdf-announces-libreoffice-25-8-7/)

本文的标签含义：**已观察**指本地只读检查或已取得的官方文本；**源码支持**指指定版本有对应实现；**建议**指项目可采用的工程组合；**待验证**指尚未使用正式包或样本确认。没有安装软件、下载完整 Office、转换文档、运行模型或解包用户测试库。两平台随包运行和四类输入转换均为**待验证**。

## 1. 版本、发行物与 Python/UNO 组合

| 平台 | 第一轮候选 | Python/UNO 与启动入口 | 当前证据 |
| --- | --- | --- | --- |
| Windows x64 | `LibreOffice_26.2.6_Win_x86-64.msi`，预期对应 26.2.6.3 | 同包 `program/python.exe` 启动 helper；同包 `soffice.com`/`soffice.exe` 启动 Office | 官方包和源码存在；未提取、未运行，完整 build ID/解释器版本须实测记录 |
| Debian x86_64 | `LibreOffice_26.2.6_Linux_x86-64_deb.tar.gz`，预期对应 26.2.6.3；在构建阶段提取到应用私有目录 | 同包 `program/python` 包装器启动 helper；同包 `program/soffice` 启动 Office | 官方包、内部 Python 构建配置和包装器源码存在；干净 Debian 13.6 上待验证 |

读取官方镜像元数据得到以下锁定候选；这里的 SHA-256 **没有通过下载整个文件重新计算**，构建任务必须验证实际下载字节：

| 发行物 | 官方大小 | 官方 SHA-256 |
| --- | --- | --- |
| Windows MSI | 373252096 bytes | `f9877032fd908beb9c0ddf06df4af5c2e85f419c42e14876c4cce5aae5fb2660` |
| Linux DEB tarball | 217285735 bytes | `fd0e8f8f2408dd2e5b90286e60f3f97cf566ba441cd48cfc5bcc68067303e0bc` |

来源：[Windows 元数据及签名入口](https://download.documentfoundation.org/libreoffice/stable/26.2.6/win/x86_64/LibreOffice_26.2.6_Win_x86-64.msi.mirrorlist)、[Linux 元数据及签名入口](https://download.documentfoundation.org/libreoffice/stable/26.2.6/deb/x86_64/LibreOffice_26.2.6_Linux_x86-64_deb.tar.gz.mirrorlist)。产品短版本不能替代解包后 `--version` 的完整版本、build ID 和文件散列；源码标签存在也不能单独证明任一下载包的内部 build ID。

26.2.6.3 的 `download.lst` 锁定 `Python-3.12.14.tar.xz`；官方 Linux 构建配置明确 `--enable-python=internal`。因此 Linux 官方 TDF 包不能与 Debian 的 `python3-uno` 拆包方式混为一谈；第一候选应复用同包解释器和桥接，而非给项目 venv 安装一个名字相似的 PyPI 包。源码中的 Windows Python 启动器设置 `UNO_PATH`、`URE_BOOTSTRAP`、`PYTHONHOME` 和 `PYTHONPATH`，然后启动 `python-core-<version>/bin/python.exe`；Linux 包装器设置同类路径后执行 `python.bin`。**3.12.14 是源码推定的候选解释器版本，仍须在发行物中读取 `sys.version` 核实。** [版本锁](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/download.lst#L614)、[Linux 构建配置](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/distro-configs/LibreOfficeLinux.conf)、[Windows 启动器](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/pyuno/zipcore/python.cxx)、[Linux 启动器](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/pyuno/zipcore/nonmac.sh)

建议将 runtime 作为一个独立目录树保存。最初验证保留官方运行时布局，不根据“只用 Writer/Impress”猜测 DLL/so 白名单。应纳入清单的类别包括：

- Office 启动器、主程序及其 native 依赖；Writer/Impress 导入组件、OOXML/旧二进制格式过滤器、PDF 导出组件和配置注册数据。
- `program` 下的 UNO runtime、`fundamental*`/`bootstrap*`、服务与类型注册数据；Python 标准库、解释器、pyuno native bridge、`uno.py`、`unohelper.py`、loader 及相应服务注册。具体 basename 和位置以目标包清单为准。
- `share` 下过滤器、配置、资源、内置字体，以及实际保留的其他组件所需资源；许可证、版权和第三方 notices。

仅复制 `soffice`、`uno.py` 或 Writer DLL 不构成官方支持的最小运行时。PyUNO 的包定义显式装入多个 Python 文件与 `pyuno.rdb`，PDF filter 注册又关联 `com.sun.star.comp.PDF.PDFFilter` 和文档服务；这证明应按完整组件依赖验证，而不是把 SDK 当作运行时替代品。[PyUNO 文件表](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/pyuno/Package_python_scripts.mk)、[PyUNO 服务打包](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/scp2/source/python/file_python.scp)、[Writer PDF filter 注册](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/filter/source/config/fragments/filters/writer_pdf_Export.xcu)

TDF 文档给出了 Windows `msiexec /a` 行政提取和 Linux `dpkg-deb -x` 私有目录布局方法；它们支持“构建阶段提取，用户随应用得到运行时”的方向，但不证明项目的重打包结果能在干净机器运行。Windows 行政提取不会自动满足正常安装时处理的 Visual C++ runtime 条件；必须盘点候选包实际导入的 DLL 及再分发权利，不能要求用户凭缺 DLL 提示自行补装。Linux 同样需验证动态依赖闭包；不要把现有桌面进程的 Qt 库目录直接用于 Office。[Windows 并行安装](https://wiki.documentfoundation.org/Installing_in_parallel/Windows)、[Linux 并行安装](https://wiki.documentfoundation.org/Installing_in_parallel/Linux)

26.2 的版本专属发布说明在 **Platform Compatibility → Linux** 明确：TDF 构建采用 AlmaLinux 9 baseline，要求 **x86-64-v2 或以上**。当前通用要求页面还列 Linux kernel ≥4.18、glibc ≥2.27，但该页面是滚动文档，不能代替 26.2.6 目标二进制的 ELF/glibc 依赖检查。现有 `package_runtime` 第 161–167 行只检查 `Linux/Windows` 与 `x86_64/amd64`，这既不证明所有旧 x86_64 CPU 可用，也不表示项目已经作出这种承诺。将候选 Office 的 CPU、系统库和 Windows runtime 最低条件纳入已有交付契约即可，无需据此新增平台。[26.2 发布说明](https://wiki.documentfoundation.org/ReleaseNotes/26.2#Linux)、[滚动系统要求](https://www.libreoffice.org/system-requirements/)、[当前打包检查](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/scripts/package_desktop.py#L161)

## 2. 子进程、profile 与退出契约

建议主应用只传输入、任务目录和结构化请求；helper 使用 Office 自带 Python，连接单独的 Office 进程。Qt 主进程不 import UNO，不向自己的 `sys.path` 加 Office，不把 Office 环境变量写入全局环境。Windows 的 Python 启动器还会创建下一层 Python 进程，因此监督范围不能只包含启动时拿到的第一个 PID。

每个受监督的 Office 实例使用独立、可写、位于任务私有目录的 `-env:UserInstallation=<file URL>`；不要共享用户日常使用的 profile，也不要依赖 `--nolockcheck` 实现隔离。建议参数为 `--headless --nologo --nodefault --norestore`，配随机命名的 `--accept=pipe,name=<id>;urp;...`，通过 `UnoUrlResolver` 连接。文件路径转为规范 file URL；参数用 argv 传递。官方明确要求 profile 可写，提供 headless、accept、norestore 和 profile 覆盖入口，但没有给出通用“转换超时秒数”。[26.2 启动参数](https://help.libreoffice.org/26.2/en-US/text/shared/guide/start_parameters.html)

官方 `officehelper.bootstrap()` 展示随机 pipe、重试和 Linux process group；它本身没有把 profile、全任务截止时间和成功路径的进程所有权完整封装给项目，因此可作为接口依据，不宜直接视作生产监督器。建议由项目监督器持有 Office/helper 的整个进程树：正常关闭文档后请求 `XDesktop.terminate()`，超时或拒绝退出则结束本任务的进程组/Job Object 并等待退出，最后清理 profile 和临时输出；不能按进程名杀掉用户自己的 Office。`terminate()` 官方接口允许被 listener 否决，所以返回成功和实际退出均需确认。[helper 源码](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/pyuno/source/officehelper.py)、[XDesktop 终止语义](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1frame_1_1XDesktop.html)

冻结应用还要处理 PyInstaller 继承环境：其官方文档说明 Linux 的 `LD_LIBRARY_PATH`、Windows 的 `SetDllDirectoryW` 设置会影响子进程。应为 Office 构造独立环境并清理 Python/Qt 的继承设置；Windows 恢复 DLL 搜索路径的方案必须避免与 Qt 主进程其他启动操作竞争。独立 bootstrap/launcher 是候选做法，具体机制待实施验证。不要把“用了 subprocess”写成“没有 ABI 污染”。[PyInstaller 外部进程约束](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application)

## 3. 导入、PDF 冻结与备注入口

官方过滤器表提供以下能力入口；这是格式支持，不是保真承诺。实际加载后还应检查返回文档服务与检测到的过滤器，不能只按后缀强制所有 OOXML 变体使用同一个过滤器。[26.2 filter 表](https://help.libreoffice.org/26.2/en-US/text/shared/guide/convertfilters.html)

| 输入 | 官方入口示例 | PDF 输出 FilterName |
| --- | --- | --- |
| DOC | `MS Word 97`；更旧 DOC 还有其他入口 | `writer_pdf_Export` |
| DOCX | `Office Open XML Text`；表中另有 Word 2007 入口 | `writer_pdf_Export` |
| PPT | `MS PowerPoint 97` | `impress_pdf_Export` |
| PPTX | `Impress MS PowerPoint 2007 XML`；另有 `Impress Office Open XML` | `impress_pdf_Export` |

helper 可用 `loadComponentFromURL` 加载，`storeToURL` 导出 PDF，外层属性指定 `FilterName`、`FilterData`。加载建议固定 `Hidden=true`、`ReadOnly=true`、`MacroExecutionMode=NEVER_EXECUTE`、`UpdateDocMode=NO_UPDATE`，以便把源文件和外部链接更新隔开；遇到密码、修复或其他交互需求应返回明确失败状态，不挂起等待不可见对话框。`ReadOnly` 不是禁止 UNO 内存修改的保证，所以不把该对象另存回输入文件。[MediaDescriptor 源码与各属性语义](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/offapi/com/sun/star/document/MediaDescriptor.idl)

以下是建议交给交付契约的**显式导出参数**，不依赖 profile 默认值；值及 UNO 类型应进入处理记录。公开帮助有参数表，26.2.6.3 源码可确认 notes、hidden slides 和 tracked changes 的实际读取。[PDF 参数帮助](https://help.libreoffice.org/26.2/en-US/text/shared/guide/pdf_params.html)、[同版 PDF exporter](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/filter/source/pdf/pdfexport.cxx)

| 项目 | 候选设置或必须明确的语义 |
| --- | --- |
| 全部页面 | 不设置 `PageRange`、`Selection`；记录实际 PDF 页数 |
| PDF 版本 | `SelectPdfVersion=17`，明确 PDF 1.7，避免未来默认改变 |
| 静态表单 | `ExportFormFields=false`，输出固定打印表示 |
| 图像 | `UseLosslessCompression=true`、`ReduceImageResolution=false`；若后续选择有损，固定 `Quality` 等参数 |
| 原文附加流 | `IsAddStream=false`，不生成带原文流的 hybrid PDF |
| 备注/评论 | `ExportNotes=false`、`ExportNotesInMargin=false` |
| Impress 备注页 | `ExportNotesPages=false`、`ExportOnlyNotesPages=false`；备注文本单独读取 |
| 隐藏幻灯片 | 用户已确认导入所有隐藏 slide，必须显式设置 `ExportHiddenSlides=true` 并记录隐藏标记；`false` 仅用于验证排除隐藏页行为的专门对照，不是待选择的交付配置 |
| 幻灯片过渡 | `UseTransitionEffects=false`；输出为静态页面，不声称复制动画播放后的任意时刻 |
| Writer 自动空白页 | 用户已要求保留排版产生的空白页；`IsSkipEmptyPages=false` 是该候选版本满足既定页序的验证配置 |
| Writer 修订 | 26.2.6.3 源码支持 `ExportTrackedChanges=false`；见下段限制 |

**最终可见 Word 正文需要单独验收。** 26.2.6.3 的 exporter 读取 `ExportTrackedChanges`，通过控制器 `PDFExport_ShowChanges` 切换 `SetHideRedlines` 并在导出后恢复；它控制修订显示，不是“接受全部修订”并改写文件。不要用 `RecordChanges=false` 冒充最终稿显示，也不要在源文件上执行接受修订。隐藏文字还需固定 Writer 文档打印设置中的 `PrintHiddenText=false`（通过文档 `com.sun.star.text.DocumentSettings`/打印属性入口，运行时检查 property 是否存在）；同版源码显示 PDF render 路径读取该打印设置。DOC/DOCX 的插入、删除、移动、隐藏段落和受保护修订都须用样本核对，未通过前只能称“源码支持该入口”。[PDF 显示切换](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/sw/source/uibase/uno/unotxvw.cxx#L688)、[PrintHiddenText 属性实现](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/sw/source/uibase/uno/unomod.cxx#L193)、[Writer PDF render 读取](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/sw/source/uibase/uno/unotxdoc.cxx#L2649)

**备注不增加 PDF 页数。** `XPresentationPage.getNotesPage()` 返回独立 notes page；在源 slide 顺序下读取该页中的文字对象，优先识别 `presentation.NotesShape`，排除幻灯片缩略图、页码等模板对象，处理空占位符及普通文本框。API 提供这些对象，但 DOC/PPT 导入后的具体对象形态仍需样本确认。输出 sidecar 应按源 slide 索引关联备注，不能把 notes page 再并入 PDF；`ExportNotes`（PDF 注释）也不能充当演讲者备注提取接口。[XPresentationPage API](https://api.libreoffice.org/docs/idl/ref/interfacecom_1_1sun_1_1star_1_1presentation_1_1XPresentationPage.html)、[NotesShape 定义](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/offapi/com/sun/star/presentation/NotesShape.idl)

“冻结”应由项目持久保存本次 PDF 字节及散列来兑现：记录后续处理实际使用的 PDF，而不是每次查询重新调用 Office。LibreOffice、字体、字段、locale 和渲染后端变化均应形成新处理身份；这里不承诺跨平台 PDF 字节、页数或像素天然一致。外部链接不更新不等于所有动态字段都不会重排，时间字段、目录、打印机度量和自动字段行为仍须样本覆盖。

## 4. 字体供给与防漂移记录

当前字体 manifest 已锁定 `Source Han Sans CN 2.005` 的 Regular/Medium/Bold，以及 `Source Code Pro 2.042` 正体和 `1.062` 斜体，每个文件有 SHA-256、家族、字重和许可文件。`desktop.spec` 将它们收进 `openkb/desktop/assets/fonts`；`register_fonts()` 只调用 `QFontDatabase.addApplicationFont`。另外渲染资产脚本还有独立的 `NotoSansCJK Sans2.004`。Office 字体输入应明确选用哪组及其文件散列，不把这些家族当成同一个版本。[manifest](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/assets/fonts/manifest.json)、[Qt 注册](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/openkb/desktop/fonts.py#L14)、[渲染字体锁](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/scripts/prepare_desktop_assets.py#L24)

建议第一轮将 manifest 中未修改的字体文件在**构建阶段**放入 Office 私有树 `share/fonts/truetype`，复用相同散列和归属，不在用户系统安装字体。26.2.6.3 Windows 字体代码在 Office 进程中遍历此目录并以 `FR_PRIVATE` 注册，也保留 `share/.../common/fonts` 内部字体；Linux 代码将该目录及用户字体目录加入 Fontconfig application font directories。虽然目录名含 truetype，Windows 路径是逐文件加载，现有 `.otf` 仍须实际验证。此处是两平台源码证据，不是已完成的随包字体验收。[Windows 字体加载](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/vcl/win/gdi/salfont.cxx#L988)、[Linux 字体路径](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/vcl/unx/generic/fontmanager/helper.cxx#L174)、[Fontconfig 注册](https://github.com/LibreOffice/core/blob/libreoffice-26.2.6.3/vcl/unx/generic/fontmanager/fontconfig.cxx#L752)

这不会自动让宋体、微软雅黑、Calibri、Cambria 等源字体拥有原字形和原度量。需要固定缺字/缺字体回退政策，并记录替换后的实际字体；不能静默把源文档所有字体改为 Source Han Sans 后宣称保真。保留 Office 内部 OpenSymbol 等资源，验证公式、项目符号、中英混排、粗斜体及 PDF 内嵌字体。字体可见性与准确命中是两个验收点。

建议处理身份至少保存：输入 SHA-256；平台/OS、CPU 最低条件；Office 完整版本/build ID、上游包 URL/SHA-256、运行时文件清单散列；同包 Python/UNO 版本与路径；helper 版本/散列；应用及 Office 自带字体清单、字体配置/替换规则版本；profile 模板版本、locale、时区和相关环境；检测到的输入 filter；全部加载/导出参数及类型；隐藏页/备注/修订政策；输出 PDF SHA-256、实际页数、slide→page 映射和诊断。重处理比较这些身份，不自动覆盖旧 PDF 或旧引用。

## 5. 对现有打包链的最小接缝

以下仅定义后续实施位置，没有修改生产代码。

| 当前入口 | 观察到的行为 | 建议接入点 |
| --- | --- | --- |
| `scripts/build_desktop.py` | 校验 source identity；准备 tiktoken；运行 PyInstaller；Windows 收窄构建 PATH | 冻结前要求已校验的 Office 输入 manifest；构建不临时探测开发机全局 Office；完成后验证运行时/字体未变 |
| `packaging/desktop/desktop.spec` | 明确收集渲染资产及字体白名单；目前没有 Office runtime | 增加独立 Office 数据树与 helper 资源接缝，保持目录布局、Linux 执行位和链接；避免将 pyuno 当主解释器 hiddenimport |
| `scripts/inventory_desktop.py` | 按 Python/npm/native/font/Debian 归属记录输入和散列；未知输入失败 | 增加 `runtime/LibreOffice`、其内置 Python、第三方组件/字体及目标包 provenance；外部 Office 文件不得误归 UrltraKB 自有源码 |
| `scripts/package_desktop.py` | 按 inventory 逐文件核验与复制；只支持 Windows/Debian x64；程序包带许可，另有材料包 | Office 必须先进入 inventory 再封装；将源码、许可、构建/提取规则接入现有材料包；更新实际平台最低条件与验收入口 |

代码来源：[build](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/scripts/build_desktop.py)、[spec](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/packaging/desktop/desktop.spec)、[inventory](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/scripts/inventory_desktop.py)、[package](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/scripts/package_desktop.py)。本研究先在原仓库使用 CodeGraph 定位，再按需要核对独立 worktree 文件。

许可文本要求与工程建议应分开：

- **官方许可事实：** LibreOffice 按 MPL-2.0 发布，并含 Apache-2.0 来源和随版本变化的其他许可；官方要求参考实际安装树 LICENSE。MPL 第 3.2 节要求发行可执行形式时提供相应源码取得方式；第 3.3 节允许 larger work 按自选条款发行，但仍须满足被覆盖代码的义务。因此不能只放一份通用 MPL 文本就认为涵盖全部 Office 内含组件。[LibreOffice 官方许可与 MPL 文本](https://www.libreoffice.org/licenses/)
- **字体许可事实：** 当前 Source Han Sans 的随附 OFL-1.1 允许随软件分发和嵌入，要求保留版权/许可，修改版本还受 Reserved Font Name 等条款约束。采用未修改字体，并分别保留所有实际字体许可；OFL 不因此要求使用这些字体生成的文档采用 OFL。[仓库随附 OFL](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/assets/fonts/SourceHanSansCN-OFL.txt)
- **项目工程建议：** 保存原始二进制包 URL、散列、签名核验结果、选定源码包及散列、LICENSE/NOTICE、第三方组件表、提取/裁剪/加字体规则和最终文件清单；与现有 companion materials 同批留存。这种具体归档形式是可追踪性建议，不能写成所有条款都强制“源码必须塞进程序包”。若额外分发 Microsoft runtime 或 Debian 库，逐项依据实际物料的再分发条款补齐记录。本研究未下载候选包，尚不能宣称完整许可证清单已完成。

## 6. 最小样本与干净机器门槛

最小内容组建议为 **四个固定文件**：一份合成 Word 源分别保存为 `.doc` 和 `.docx`，一份合成演示源分别保存为 `.ppt` 和 `.pptx`；它们不是用户测试库。每个文件的输入字节、预期内容标记和人工核对基准都锁定。二进制版和 OOXML 版须分别验收，不能用一对格式的通过代替另一对。

| 样本组 | 必含内容 | 最低观察 |
| --- | --- | --- |
| DOC 与 DOCX 各一份 | 中文/英文、常用与缺失字体、粗斜体；分页/分节、页眉页脚、表格跨页、浮动图片、公式；插入/删除/移动修订、隐藏文字、评论、日期字段与外链 | 最终可见正文没有删除文字/隐藏标记泄漏；应见文字、表格、图和公式不丢失；源文件散列不变；PDF 可读且页数进入记录 |
| PPT 与 PPTX 各一份 | 三张源 slide，其中一张隐藏；每张不同备注标记，至少一张空备注/模板对象；中文字体、图表/图片、动画/媒体占位 | 既定配置 `ExportHiddenSlides=true` 应得到 3 个 slide 页面；专门对照设为 `false` 时应为 2 个；notes 关闭时均无额外备注页；sidecar 按源索引对应，空占位符不误识别；静态画面与缺失能力有记录 |
| 小型异常变体 | 密码文件、截断文件、外链不可达、超时或取消 | 不弹不可见对话框、不无限等待、不发布半成品、不遗留本任务子进程 |

干净机器门槛是 Windows x64 与 Debian 13.6 x86_64 **各自**从最终 archive 开始：机器无独立 Office、无项目 Python/UNO、无开发工具路径和应用中文字体；普通用户、中文及空格路径、应用目录只读而任务目录可写、网络断开。能启动同包 helper/UNO、导出四类文件、读备注、确认字体命中；任务结束和强制取消后进程/临时目录可回收。再检查已安装另一份 Office 时并行运行不复用用户 profile，并验证两个任务相互隔离。

验收必须从冻结主程序入口启动，不能只手工执行 `program/soffice`；记录 native 依赖诊断与 Office/Python 实际版本。PDF 检查至少包括页数、预期文字标记、字体列表和代表页视觉检查。对同一固定平台/运行时重复导出，检查页映射和内容稳定；不要求 PDF 时间戳等元数据字节完全一致，也不要求 Windows 与 Linux 页数/像素相同。

## 7. 已观察、待验证与决策影响

本机只读结果：`soffice --version` 为 `LibreOffice 25.2.3.2 520(Build:2)`；`dpkg-query` 中 `libreoffice-core`、`libreoffice-writer`、`libreoffice-impress`、`python3-uno` 均为 `4:25.2.3-2+deb13u7`；项目 `.venv` 为 Python 3.12.13，`importlib.util.find_spec("uno")` 返回 `None`。这只说明系统包存在且当前 venv 找不到 UNO，不证明系统 UNO 不可用，更不证明正式桌面包已携带 Office。

尚未验证的必要项：

1. 两个候选官方包的实际 build ID、Python 版本、提取后可迁移性、完整文件和许可清单。
2. Windows native runtime 与 Debian 动态依赖闭包、CPU/OS 最低条件；冻结应用对子进程的环境隔离。
3. 字体可见性、实际选择/替换、PDF 嵌入和公式/符号完整性。
4. DOC/DOCX 的最终修订显示、隐藏文字、字段及分页；PPT/PPTX 的隐藏页、备注对象识别、静态动画/媒体表示。
5. 干净机器、只读程序目录、并发、已有 Office 共存、超时取消和全进程树清理。

**未发现必须新增独立产品决策票的问题。** 精确发行物选择、Linux x86-64-v2 条件、native runtime、显式导出参数和验证门槛交给[《决策：多格式方案达到什么依赖与验收条件才可交付实施？》](https://github.com/wooveep/UrltraKB/issues/72)。已确认隐藏页纳入且 Writer 空白页保留，[《决策：PDF 与 Markdown 如何冻结最小共同索引和回读契约？》](https://github.com/wooveep/UrltraKB/issues/70)只细化页映射的实现与断言。若后续实际验收发现无法满足既定交付边界，再用具体失败证据提出分支决策。本票可以在这些事实与限制交接后结束研究，不以未做的发行包实测冒充结论。
