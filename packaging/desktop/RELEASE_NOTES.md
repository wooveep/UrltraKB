# UrltraKB 1.2.0

基于 `dev-1.2.0` 发布原生桌面工作台、命令行和独立 REST API。

## 本次更新

- 原始文档导入、Wiki 知识编译、PageIndex 长文检索与持久化对话。
- PageIndex、ChatIndex、ConDB、LiteLLM 使用项目内锁定的 SDK 源码。
- 文档索引以 ConDB 文件保存；共享模型配置、用量统计与任务恢复。
- CNKI 文档转换、来源阅读、知识与产品版本管理。
- 从 `dev-1.1.0` 移植四平台自动构建、安装包验收和发布校验。

## 下载与安装

| 平台 | 安装包 | 使用方式 |
| --- | --- | --- |
| Debian amd64 | `UrltraKB-1.2.0-debian-amd64.deb` | `sudo apt install ./UrltraKB-1.2.0-debian-amd64.deb` |
| Debian arm64 | `UrltraKB-1.2.0-debian-arm64.deb` | `sudo apt install ./UrltraKB-1.2.0-debian-arm64.deb` |
| Windows x64 | `UrltraKB-1.2.0-windows-x64.zip` | 解压后运行 `UrltraKB.exe`，保留完整目录 |
| macOS arm64 | `UrltraKB-1.2.0-macos-arm64.zip` | 解压后将 `UrltraKB.app` 移至 Applications |

附件同时提供各平台对应的 `-source.zip`、`-build.json` 和统一的
`SHA256SUMS.txt`，全部绑定 `v1.2.0` 的同一提交。程序内含 Python 运行时，
无需另行安装 Python；模型服务通过应用设置配置。

## 运行要求与验证范围

- Debian 13 或兼容系统（glibc 2.41+）；macOS 为 Apple Silicon、macOS 14+。
- macOS 使用 ad-hoc 签名，未经 Apple 公证；首次启动可能需要在“隐私与安全性”中允许。
- 四个平台执行安装包提取及冻结程序自动验收；Debian 额外验证实际安装与卸载。
- Linux/macOS 自动验收使用 Qt offscreen，不替代实机窗口、菜单和首次启动检查。
- 应用源码和构建清单随附件提供；完整第三方源码与许可证材料审计范围另见构建文档。

详见[桌面构建说明](https://github.com/wooveep/UrltraKB/blob/v1.2.0/packaging/desktop/README.md)。
