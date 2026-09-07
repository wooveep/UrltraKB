# 完整依赖便携运行原型（可丢弃）

本目录只回答 [原型：完整依赖与工作进程能否在目标系统免安装运行？](https://github.com/wooveep/UrltraKB/issues/7)。
先读 [REPORT.md](REPORT.md)；当前有 Debian 实测，Windows 未构建、未运行。
原型位于独立 `codex/portable-runtime-prototype` 分支，不修改产品核心和正式入口。

## 用户运行

本机生成的 `artifacts/OpenKBProbe-Linux-x86_64.tar.gz` 解压后，双击 `OpenKBProbe/OpenKBProbe`。
程序默认把所有试验数据、试验配置放在系统临时目录 `OpenKB-PROTOTYPE-*`，可以删除。
窗口自动运行探针，可选择 PNG、关闭窗口或真正退出。
当前 GNOME 没有可用托盘，关闭窗口保持可见；不自动恢复已退出任务。

也可以指定一个尚未使用的输出目录：

```sh
./OpenKBProbe --output "/tmp/OpenKB 测试一" --auto-exit
```

这是当前用户本地的技术验证包，未作为 GitHub Release 发布。程序不需要用户的 API Key。
模型传输连接本机虚拟响应，**不能据此认为真实模型、TLS、代理或云端 PageIndex 已验收**。

## 构建输入

- 产品代码：渲染原型基线 `e581f76519dbda8b6cfdc485b8b06bb0c7742696`。
- 核心依赖：[core-requirements.txt](core-requirements.txt)，由既有 `uv.lock` 使用 `uv export --frozen --no-dev --no-emit-project` 导出，含哈希和平台条件；核心 pin 不变。
- 构建与 GUI：[tooling.in](tooling.in)、[tooling-requirements.txt](tooling-requirements.txt)，精确版本及跨平台哈希锁。
- CPython 3.12.13、PyInstaller 6.22.2、hooks-contrib 2026.7、PySide6-Essentials/shiboken6 6.11.2。
- Node 24.20.0、MathJax/NewCM 4.1.3、Merman/merman-render 0.7.0、resvg 0.46.0、rustybuzz 0.20.1。沿用相邻 [渲染原型](../desktop_prototype/README.md) 的 npm/Cargo 锁与资源校验。
- hatchling 1.32.0、hatch-vcs 0.5.0 仅构建 wheel；xlwt 1.3.0 仅生成旧版 XLS 样本。

程序冻结入口先 `multiprocessing.freeze_support()`，再加载 Qt；工作进程使用显式 `spawn`。
从 wheel 收集核心资源以包含内置 Skill，`_internal/probe_assets` 存放随包 renderer、字体、词表与样本。
外部 helper 启动前恢复 Linux 的原始动态库搜索环境。

## Debian 构建

构建机器需要 uv、Rust/Cargo、常规构建工具；测试机器不需要它们。
下面从仓库根目录执行。Cargo.lock 必须保持原样。

```sh
uv venv --python 3.12.13 openkb/portable_prototype/.venv
uv pip install --python openkb/portable_prototype/.venv/bin/python --require-hashes \
  -r openkb/portable_prototype/core-requirements.txt \
  -r openkb/portable_prototype/tooling-requirements.txt
openkb/portable_prototype/.venv/bin/python openkb/desktop_prototype/bootstrap.py
cargo build --locked --manifest-path openkb/desktop_prototype/rust-helper/Cargo.toml
openkb/portable_prototype/.venv/bin/python openkb/portable_prototype/prepare_assets.py \
  --renderer-source openkb/desktop_prototype
openkb/portable_prototype/.venv/bin/python openkb/portable_prototype/build.py
openkb/portable_prototype/.venv/bin/python openkb/portable_prototype/package.py
```

如果相邻渲染原型已经按固定版本准备好，可将 `--renderer-source` 指向那个目录。
本轮使用既有渲染工作树的已校验资源，没有重新下载不同版本。
`build.py` 把产品文件原样复制到临时 wheel 构建目录，过滤原型缓存和构建产物，避免嵌套打包；不修改仓库 `pyproject.toml`。

## 干净 Debian 运行验证

`Dockerfile.verify` 固定 Debian 13.6 slim 镜像 digest，安装桌面系统库，不安装 Python、Node、Qt 开发包或编译器。
镜像的实际包版本见 [container-packages.txt](evidence/container-packages.txt)。容器普通用户 UID 1000。

```sh
docker build -f openkb/portable_prototype/Dockerfile.verify \
  -t openkb-portable-probe-debian:13.6 openkb/portable_prototype
openkb/portable_prototype/.venv/bin/python openkb/portable_prototype/verify_linux.py \
  --bundle "/解压后的目录/OpenKBProbe" --label extracted
```

验证脚本将程序只读挂载到 `/opt/便携 应用/OpenKBProbe`，结果独立挂载到 `/data/测试结果`，cwd 为 `/tmp`。
只共享宿主 X11 socket 与授权文件，运行网络为 `none`；容器仍共享宿主内核和 GNOME/X11 显示服务。
它证明干净用户空间中的依赖可用，不等于另一台独立 GNOME 机器或文件管理器解压流程已验收。
默认不接入宿主 D-Bus，强制无托盘回退；本轮另在真实宿主运行探针，Qt 同样报告无可用托盘。

仅这个一次性容器传 `--use-system-profile`，验证核心原有 `/home/probe/.config/openkb` 路径；
默认交互运行在原型适配器中把配置常量指向 scratch 目录，防止将测试知识库登记进使用者真实配置。
两种模式均不改变 `HOME`，产品配置代码未改动。

## Windows 构建与待验收步骤

**以下是待验证命令，不是已得到的 Windows 证据。** Windows 11 x64 构建机需 Python/uv、Rust MSVC 工具链和相应 C++ 构建工具。
构建机与干净运行机器是两个角色；目标运行机器不能靠预装运行时补齐缺 DLL。

在 PowerShell 的仓库根目录执行：

```powershell
uv venv --python 3.12.13 openkb/portable_prototype/.venv
uv pip install --python openkb/portable_prototype/.venv/Scripts/python.exe --require-hashes -r openkb/portable_prototype/core-requirements.txt -r openkb/portable_prototype/tooling-requirements.txt
& openkb/portable_prototype/.venv/Scripts/python.exe openkb/desktop_prototype/bootstrap.py
cargo build --locked --manifest-path openkb/desktop_prototype/rust-helper/Cargo.toml
& openkb/portable_prototype/.venv/Scripts/python.exe openkb/portable_prototype/prepare_assets.py --renderer-source openkb/desktop_prototype
& openkb/portable_prototype/.venv/Scripts/python.exe openkb/portable_prototype/build.py
& openkb/portable_prototype/.venv/Scripts/python.exe openkb/portable_prototype/package.py
```

预期生成 `artifacts/OpenKBProbe-Windows-x86_64.zip`。该文件本轮尚不存在。
将整包复制到无 Python/Node/开发工具的 Windows 11 x64 普通用户环境，解压到含中文和空格的路径。
双击 EXE，观察是否启动一次、没有递归新窗口；检查文档处理、所有 PNG、托盘隐藏/恢复、真正退出及残留进程。
默认会创建独立临时配置；验证既有配置路径须使用一次性 Windows 测试账户再传 `--use-system-profile`。
返回 Windows 版本、包 SHA256、`summary.json`、截图和错误记录；不要提供 API Key。
若缺 VC/DLL、存在控制台闪烁或 helper 搜索路径冲突，应修正构建后重新验证，不要求普通用户安装开发环境绕过问题。

## 探针和证据的边界

本原型没有实现桌面任务队列、跨入口完整会话锁、生产配置快照协议或目录监听的新事件语义。
“停止”只在逐项写入探针中验证；耗时依赖任务完成当前阶段后才能退出。
更多范围和待决事项见 [REPORT.md](REPORT.md)。后续正式发布流水线与许可材料不属于本原型。
