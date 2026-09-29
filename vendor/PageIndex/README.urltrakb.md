# PageIndex 本地源码依赖

此目录基于 [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex.git)
标签 `v0.3.0.dev3`，提交 `9ad54122bbd519cec8913198e2d63cff92781c1e`。
上游 `.git` 已删除，所有文件由 UrltraKB 仓库管理；保留上游
[MIT 许可证](LICENSE)和作者信息。

当前本地版本为 `0.3.0.dev3+urltrakb.2`，仅提供本地索引和检索。
已删除托管服务后端、旧版远程 SDK、云端客户端和相关示例；
`PageIndexClient` 与 `LocalClient` 都使用本地后端。
原来的服务密钥参数不再接受，环境中残留的 `PAGEINDEX_API_KEY` 不会启用
云端索引。PDF 解析、索引数据库和源文件存储均在本地执行。
模型推理仍使用项目配置的 LLM 服务商；需要完全本地推理时配置本地模型。

选择该上游标签是为了保留 dev-1.2.0 的原始文档导入和对话方案。
本地索引、解析、存储和检索算法保持上游实现。来源信息、导入前文件
SHA256、本地修改及删除清单见 [UPSTREAM.json](UPSTREAM.json)。
上游升级需要显式引入、核对本地补丁并重新验证。

物理页回读补丁（#81）保留空白 PDF 页、独立印刷页码、受管输入及图片摘要，
并为全文与范围读取提供同一缓存恢复路径。缓存身份包含解析与索引处理参数；
SQLite 仅增加可选元数据列，旧记录不推断覆盖完整性。PDF 内容索引与递归算法保持原样。

## 安装和运行

在 UrltraKB 根目录执行：

```sh
uv sync --frozen --extra desktop --extra api --extra dev
uv run openkb-desktop
```

`uv` 以 editable 方式安装本目录。修改 Python 文件后，新进程直接使用
修改后的内容；长期运行的桌面或服务需重启。若使用 pip：

```sh
pip install -e ./vendor/PageIndex -e ".[desktop,api,dev]"
```

桌面和 CLI 均使用本地导入：`openkb add <文件、目录或网址>`。
已取消按云端文档 ID 导入。历史导入资料的本地 Wiki、来源正文和删除操作
继续受支持，不会请求远端原文。

## 构建

原生桌面构建验证 PageIndex 导入路径属于同一源码树，拒绝使用其他 checkout
或 PyPI 版本。应用源码导出和 sdist 包含此目录。

```sh
uv build vendor/PageIndex --wheel --out-dir dist
uv build --wheel --out-dir dist
```

分发时同时提供两者，以 `pip install --find-links /path/to/dist openkb` 安装。
构建后端固定为 `poetry-core==2.4.1`，本地版本后缀避免误装同名 PyPI 包。
原生桌面完整构建方法见仓库的 `packaging/desktop/README.md`。
