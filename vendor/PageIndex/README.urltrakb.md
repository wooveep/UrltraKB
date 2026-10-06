# PageIndex 本地源码依赖

此目录基于 [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex.git)
标签 `v0.3.0.dev3`，提交 `9ad54122bbd519cec8913198e2d63cff92781c1e`。
上游 `.git` 已删除，所有文件由 UrltraKB 仓库管理；保留上游
[MIT 许可证](LICENSE)和作者信息。

当前本地版本为 `0.3.0.dev3+urltrakb.6`，仅提供本地索引和检索。
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

内容块补丁（#84，`+urltrakb.3`）给原内容算法增加显式块定位策略：原文/生成标题
分别验证真实锚点，块不套用 PDF 印刷页码与目录中点规则。结构转换保留定位元数据，
原递归 AND 条件、节点原文摘要、描述及完整树保持。OpenKB 在共同客户端工厂注册
冻结包解析器，缓存与恢复共用受管包；`get_block_content` 与物理页接口区分。

幻灯片补丁（#89，`+urltrakb.4`）在固定 PDF 旁冻结原页备注与映射。`.okpi`
包装器仍调用 PDF 解析器和同一内容算法；备注属于原物理页的独立 notes 域。
生成导航保存经原文验证的 body/notes 锚点，缓存恢复验证完整分域内容及图片。
普通 PDF 不启用该定位策略；已知物理页数通过 `page_count` 返回。

`.5` 已包含固定输入的恢复与幻灯片读回修正。本次四包纳管仅校正来源记录、
固定依赖并添加包内身份资源，未修改 PageIndex 算法源码，因此保持 `.5`。
包版本与内容策略兼容性分开判断，不能仅因安装版本变化要求重建旧索引。

## 安装和运行

在 UrltraKB 根目录执行：

```sh
uv sync --frozen --extra desktop --extra api --extra dev
uv run openkb-desktop
```

`uv` 以 editable 方式安装本目录。修改 Python 文件后，新进程直接使用
修改后的内容；长期运行的桌面或服务需重启。若使用 pip：

```sh
pip install -e ./vendor/PageIndex -e ./vendor/ChatIndex -e ./vendor/ConDB -e ./vendor/LiteLLM -e ".[desktop,api,dev]"
```

桌面和 CLI 均使用本地导入：`openkb add <文件、目录或网址>`。
已取消按云端文档 ID 导入。历史导入资料的本地 Wiki、来源正文和删除操作
继续受支持，不会请求远端原文。

## 构建

原生桌面构建验证四个 vendor 导入路径和来源摘要属于同一源码树，拒绝使用其他 checkout
或 PyPI 版本。应用源码导出和 sdist 包含此目录。

```sh
uv build vendor/LiteLLM --wheel --out-dir dist
uv build vendor/PageIndex --wheel --out-dir dist
uv build vendor/ChatIndex --wheel --out-dir dist
uv build vendor/ConDB --wheel --out-dir dist
uv build --wheel --out-dir dist
```

分发时同时提供五个 wheel，以 `pip install --find-links /path/to/dist openkb` 安装。
构建后端固定为 `poetry-core==2.4.1`，本地版本后缀避免误装同名 PyPI 包。
原生桌面完整构建方法见仓库的 `packaging/desktop/README.md`。

运行时注入补丁（#111）提供 IndexLLM 同步/异步窄协议及不序列化的
IndexConfig.llm_client。OpenKB 工厂强制注入；独立客户端仍可显式使用
默认 LiteLLM。读取与存储不校验模型凭据，网络重试归调用方执行器；
必需结构步骤异常会取消并等待同批任务，不再转换为空结果。
SDK 来源版本、pageindex.document.v1 格式和 content-based-physical-v1
内容策略分别保存。成功树内容兼容 +urltrakb.5，不因接口升级付费重建。
