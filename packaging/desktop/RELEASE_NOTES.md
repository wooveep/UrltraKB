# UrltraKB 1.0.0

首个正式版本，提供原生桌面工作台、命令行和独立 REST API。

## 主要功能

- 文档导入、PageIndex SQLite 索引、知识编译与来源引用。
- 持久化对话、原文证据核对、任务进度和中断恢复。
- 原生 Markdown、公式与图表阅读，浅色／深色主题及分层设置。
- 每个系统用户仅运行一个桌面实例，重复启动恢复现有窗口。

## 四平台下载

| 平台 | 安装包 | 使用方式 |
| --- | --- | --- |
| Debian amd64 | `UrltraKB-1.0.0-debian-amd64.deb` | `sudo apt install ./UrltraKB-1.0.0-debian-amd64.deb` |
| Debian arm64 | `UrltraKB-1.0.0-debian-arm64.deb` | `sudo apt install ./UrltraKB-1.0.0-debian-arm64.deb` |
| Windows x64 | `UrltraKB-1.0.0-windows-x64.zip` | 解压后运行 `UrltraKB.exe` |
| macOS arm64 | `UrltraKB-1.0.0-macos-arm64.zip` | 解压后将 `UrltraKB.app` 移至 Applications |

四个平台完成自动构建、安装包提取检查及原生应用验收后，自动上传安装包、
各平台匹配的 `-source.zip`、`-build.json` 和统一的 `SHA256SUMS.txt`。
附件尚未出现时，可在 [GitHub Actions](https://github.com/wooveep/UrltraKB/actions/workflows/desktop-build.yml)
查看构建进度。所有附件绑定同一 `v1.0.0` 提交。

## 运行要求与范围

- Debian 13 或兼容的更新系统；macOS 为 Apple Silicon、macOS 14 及以上。
- macOS 应用使用 ad-hoc 签名，尚未进行 Apple 公证。
- 可选 OCR 引擎和模型权重单独安装；模型服务需要自行配置。
- 自动验收使用受控样本；实际模型回答与 OCR 质量仍取决于材料及服务。
- 附件包含对应应用源码和构建清单；完整第三方源码／许可证材料审计另行记录，
  自动构建不表示该审计已完成。

详见 [桌面构建说明](https://github.com/wooveep/UrltraKB/blob/v1.0.0/packaging/desktop/README.md)。
