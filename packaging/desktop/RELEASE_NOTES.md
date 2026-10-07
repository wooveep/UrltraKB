# UrltraKB 1.2.1

基于 `dev-1.2.0` 发布原生桌面工作台、命令行和独立 REST API。

## 本次更新

- macOS Apple Silicon 新增 Word（DOC、DOCX）和 PowerPoint（PPT、PPTX）转换，内置完整锁定的 LibreOffice、私有 Python/UNO 和字体，无需安装 Microsoft Office。
- 保留 Word 物理页、PowerPoint 隐藏幻灯片及演讲备注，继续使用统一的来源阅读与知识编译流程。
- macOS 转换进程独立管理，应用被强制结束后清理转换子进程。
- 安装包验收增加四种 Office 格式的实际转换，并检查页码、隐藏页与备注。

## 下载与安装

| 平台 | 安装包 | 使用方式 |
| --- | --- | --- |
| Debian amd64 | `UrltraKB-1.2.1-debian-amd64.deb` | `sudo apt install ./UrltraKB-1.2.1-debian-amd64.deb` |
| Debian arm64 | `UrltraKB-1.2.1-debian-arm64.deb` | `sudo apt install ./UrltraKB-1.2.1-debian-arm64.deb` |
| Windows x64 | `UrltraKB-1.2.1-windows-x64.zip` | 解压后运行 `UrltraKB.exe`，保留完整目录 |
| macOS arm64 | `UrltraKB-1.2.1-macos-arm64.zip` | 解压后将 `UrltraKB.app` 移至 Applications |

附件同时提供各平台对应的 `-source.zip`、`-build.json` 和统一的
`SHA256SUMS.txt`，全部绑定 `v1.2.1` 的同一提交。程序内含 Python 运行时，
无需另行安装 Python；模型服务通过应用设置配置。

## 运行要求与验证范围

- Debian 13 或兼容系统（glibc 2.41+）；macOS 为 Apple Silicon、macOS 14+。
- Windows x64、Debian amd64 与 macOS arm64 内置锁定的 LibreOffice，支持 Word/PPT 转换；Linux Office 需要 x86-64-v2 CPU。
- Debian arm64 暂不支持 Office 转换，可导入 PDF、文本和工作簿。macOS 尚未包含用于恢复 CFB 内嵌附件的辅助工具。
- macOS 使用 ad-hoc 签名，未经 Apple 公证；首次启动可能需要在“隐私与安全性”中允许。
- 四个平台执行安装包提取及冻结程序自动验收；Debian 额外验证实际安装与卸载。
- Linux/macOS 自动验收使用 Qt offscreen，不替代实机窗口、菜单和首次启动检查。
- 应用源码和构建清单随附件提供；完整第三方源码与许可证材料审计范围另见构建文档。

详见[桌面构建说明](https://github.com/wooveep/UrltraKB/blob/v1.2.1/packaging/desktop/README.md)。
