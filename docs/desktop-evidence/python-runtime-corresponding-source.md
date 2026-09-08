# Python 运行时对应源码与许可材料

日期：2026-09-08。范围：冻结程序 `87bdcc7b6e3306b2f1981b80af5a705b258e0c35` 的 CPython 运行时及其嵌入依赖；这份材料不替代整个应用的发行审查。

本次闭合了双端 Python 来源差异、静态依赖、构建补丁及原始许可映射。机器清单见 [python-runtime-corresponding-source.json](python-runtime-corresponding-source.json)。13 个源码归档、两个固定 PBS 构建源码归档均已保存；55 项许可及原始版权材料集中于 `packaging/desktop/build/source-cache/python-runtime-resolution/python-runtime-licenses.zip`，SHA256 为 `80c6d5a48ea3ae448bd3b5d10c2df46de778cbb3838df8b24dec6d589f9bad5a`。

## 程序来源

| 平台 | 实际 PBS 发行 | PBS 源码提交 | 哈希核验 |
| --- | --- | --- | --- |
| Linux x86_64 | 20260623，pgo+lto | `7f93a7d443d97f08e3afc6e24a3c675e9994b6aa` | 实际 `libpython3.12.so.1.0` 与官方精简归档完全一致 |
| Windows x86_64 | 20260807，pgo | `00c8a06113f11220667c3bcf5fab1672ff9e78ef` | 27 个实际 DLL/PYD 与官方精简归档逐一完全一致，包括两项单独映射的微软运行库 |

已下载两次发行的完整归档，提取 `PYTHON.json`、构建配置、对象文件清单及原始许可证。完整归档与精简归档均按 GitHub 官方 asset digest 验证；二进制逐文件结果记录在 JSON。完整二进制归档仅作为来源证据，不冒称为源码归档。[PBS 20260623](https://github.com/astral-sh/python-build-standalone/releases/tag/20260623)、[PBS 20260807](https://github.com/astral-sh/python-build-standalone/releases/tag/20260807)、[归档格式说明](https://gregoryszorc.com/docs/python-build-standalone/20260510/distributions.html)。

两端均使用 Python 3.12.13 原始发行 tarball，其 SHA256 与两个 PBS 提交的下载表一致。Linux 和 Windows 的构建脚本、条件补丁、依赖构建脚本、下载摘要保留在各自固定 PBS 源码归档中；没有本地修改 Python 运行时源码。PyInstaller 对标准库的冻结/收集由主清单中的应用源码、spec 和构建脚本解释。[Python 3.12.13 源码](https://www.python.org/ftp/python/3.12.13/Python-3.12.13.tar.xz)、[Linux 构建脚本](https://github.com/astral-sh/python-build-standalone/blob/7f93a7d443d97f08e3afc6e24a3c675e9994b6aa/cpython-unix/build-cpython.sh)、[Windows 构建脚本](https://github.com/astral-sh/python-build-standalone/blob/00c8a06113f11220667c3bcf5fab1672ff9e78ef/cpython-windows/build.py)。

## 实际嵌入依赖

| 组件 | Linux | Windows | 许可 |
| --- | --- | --- | --- |
| bzip2 | 1.0.8，静态 | 1.0.8，`_bz2.pyd` | bzip2 |
| expat | 2.8.1，静态 | CPython 内置 2.7.4 | MIT |
| libffi | 3.4.6，静态 | CPython 补丁版 3.4.2，提交 `16fad4855b3d8c03b5910e405ff3a04395b39a98` | MIT |
| mpdecimal | 4.0.0，静态 | CPython 内置 2.5.1 | BSD-2-Clause |
| libedit | 20240808-3.1，静态 | 无 | BSD-3-Clause |
| ncurses | 6.5，静态 | 无 | X11 |
| OpenSSL | 3.5.7，静态 | 3.5.7，独立 DLL | Apache-2.0 |
| SQLite | 3.53.1，静态 | 3.53.1，独立 DLL | 公有领域声明 |
| libuuid | 1.0.3，静态 | 无，使用系统 `rpcrt4` | BSD-3-Clause |
| liblzma | xz 5.8.3，静态 | xz 5.8.3，`_lzma.pyd` | 0BSD |
| zlib | 1.3.2，静态 | 1.3.2，内置模块 | Zlib |

上述来源通过实际运行时版本、内置模块表、哈希匹配后的构建元数据与固定下载表共同核对；Windows libffi 使用脚本明确检出的补丁提交。原始归档及版本特定许可随源清单保留。[Linux 下载表](https://github.com/astral-sh/python-build-standalone/blob/7f93a7d443d97f08e3afc6e24a3c675e9994b6aa/pythonbuild/downloads.py)、[Windows 下载表](https://github.com/astral-sh/python-build-standalone/blob/00c8a06113f11220667c3bcf5fab1672ff9e78ef/pythonbuild/downloads.py)、[Windows libffi 源码](https://github.com/python/cpython-source-deps/tree/16fad4855b3d8c03b5910e405ff3a04395b39a98)。

CPython 还自带 HACL* 哈希实现、KreMLin 支持头文件和 BLAKE2 实现。源码、更新脚本及原始头部声明保留在 Python 源码内；二进制许可材料补入 HACL* 的 MIT 声明、KreMLin 的 Apache-2.0 声明及全文、BLAKE2 的 CC0 声明及全文。综合 Python 许可附录也完整保留。[HACL* 源码](https://github.com/python/cpython/tree/3bb231a6a5dc02b95658877318bf61501a7209e9/Modules/_hacl)、[BLAKE2 源码](https://github.com/python/cpython/tree/3bb231a6a5dc02b95658877318bf61501a7209e9/Modules/_blake2)、[Python 许可附录](https://github.com/python/cpython/blob/3bb231a6a5dc02b95658877318bf61501a7209e9/Doc/license.rst)、[CC0 原文](https://creativecommons.org/publicdomain/zero/1.0/legalcode.en)。

没有把 PBS 元数据中保守合并的 OpenSSL 1.1、zlib-ng 许可名称当成实际存在的替代组件，也没有把 Windows `_uuid` 标注泛化为包含 libuuid。`_dbm`、`_tkinter` 等未随实际程序收集的可选共享扩展不进入 Python 运行时清单；应用其他部分实际收集的 X11、Qt 或 Debian 库仍由主清单分别处理。PBS 构建脚本按自身 MPL-2.0 条款保存，不能把该条款泛化到 Python 输出程序。[PBS 固定源码](https://github.com/astral-sh/python-build-standalone/tree/00c8a06113f11220667c3bcf5fab1672ff9e78ef)。

## 微软运行库的独立条款

实际 `VCRUNTIME140.dll` 和 `VCRUNTIME140_1.dll` 均为 `14.44.35211.0`。两者不仅匹配 PBS 归档，还匹配已安装 Visual Studio Community 2026 `18.8.12023.21` 的 `VC/Redist/MSVC/14.44.35112/x64/Microsoft.VC143.CRT`，属于普通可再分发目录；未使用 `debug_nonredist`。原始文件哈希及官方 Redist 指针保存在机器清单内。

官方再分发清单允许在 Visual Studio 许可条件下随应用复制这些未修改的运行库；Community EULA 包含下游保护条件。发行声明必须保留微软运行库的独立条款、原始声明与下游保护要求，不能把 DLL 标成 AGPL 或声称具有其源码再分发权。Runtime EULA 自身不是独立的再分发授权；授权依据是已安装 Visual Studio 的 Distributable Code 条款及其清单。许可 ZIP 仅包含可读文本；原始 Microsoft DOCX、2026/2022 再分发页面、190 字节的原始 Redist 指针文件另列为 build/provenance 材料。匹配收据仅保留版本、两个 DLL 的哈希和相对目录，不包含整机或用户资料。[Community 2026 条款](https://visualstudio.microsoft.com/license-terms/vs2026-ga-community/)、[2026 再分发清单](https://learn.microsoft.com/en-us/visualstudio/releases/2026/redistribution)、[V14 运行库条款](https://visualstudio.microsoft.com/license-terms/vs2026-ga-visualcpp-v14-redist-runtime/)。

系统库源码例外与微软再分发权是两件事：系统运行库可以按 AGPL 第 1 节的实际定义单独判断源码范围；这不会撤销微软运行库自身的许可条件，也不能推导“所有第三方库都无需源码”。未随包提供的 glibc/Win32 系统接口与实际随包提供的第三方库分开映射。[AGPL 第 1 节](https://www.gnu.org/licenses/agpl-3.0.html#section1)。

## 装配要求

- 将 JSON 中的 13 个源码归档与双端固定 PBS 源码归档纳入对应源码材料；保留摘要、补丁和构建配置。
- 将许可 ZIP 中的原始声明、可读文本与微软独立条款纳入发行材料；没有把源码缓存的存在等同于正式发行材料已经装配。
- PBS 完整二进制归档只用于溯源，不因归档内含可选对象文件而把它们加入实际运行时 SBOM。
- 此项仅补全材料映射，程序字节未改变；最终发行材料、下载入口及应用内许可入口由整体交付流程验收。
