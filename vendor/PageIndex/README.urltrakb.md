# PageIndex 源码依赖

此目录是 [VectifyAI/PageIndex](https://github.com/VectifyAI/PageIndex.git) 的
`v0.3.0.dev3` 源码快照，提交 `9ad54122bbd519cec8913198e2d63cff92781c1e`。
上游 `.git` 已删除；这里不是子模块，所有内容由 UrltraKB 的 Git 仓库管理。
保留上游的 [MIT 许可证](LICENSE)、文档、示例和测试。

选择该标签是为了保留 dev-1.2.0 的原始文档导入和对话方案。其 `pageindex/`
内容已逐文件核对，与此前安装的 PyPI `0.3.0.dev3` 完全一致。
上游发布流程在构建时把 Git 标签写入版本字段；源码里的 `0.3.0.dev1` 是
占位值。因此这里将包版本固定为 `0.3.0.dev3+urltrakb.1`，同时固定原发布轮子
使用的构建后端 `poetry-core==2.4.1`。本地版本后缀避免误装同名 PyPI 包。

来源信息、导入前每个文件的 SHA256 以及本地改动说明见 [UPSTREAM.json](UPSTREAM.json)。
它用于来源核对，不是自动更新配置；上游变更需要显式引入并重新验证。

在 UrltraKB 根目录执行 `uv sync --frozen --extra desktop --extra api --extra dev`，
将以 editable 方式安装本目录。修改这里的 Python 文件后，启动的新进程直接
使用修改后的内容；长期运行的桌面或服务应重启。

原生桌面构建会验证 PageIndex 导入路径属于同一源码树，拒绝使用其他 checkout
或 PyPI 安装的版本。应用源码导出和 sdist 包含本目录。若使用 pip，应同时安装
两个项目：`pip install -e ./vendor/PageIndex -e ".[desktop,api,dev]"`。

构建离线分发用的两个 Python 轮子：

```sh
uv build vendor/PageIndex --wheel --out-dir dist
uv build --wheel --out-dir dist
```

分发时同时提供两者，以 `pip install --find-links /path/to/dist openkb` 安装。
原生桌面完整构建方法见仓库的 `packaging/desktop/README.md`。
