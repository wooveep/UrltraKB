# Rust 候选依赖许可补全

2026-09-08。范围：`source-cache/rust-locks.json` 的 8 份锁文件、885 个已缓存 registry crate，以及 `license-evidence/extended-evidence.json` 中没有独立许可证文件的条目。

本轮补齐 **23 个候选 crate 的完整许可文本与原始声明映射**，复用此前完成的 7 个 renderer crate。新增材料位于 `packaging/desktop/build/source-cache/rust-license-resolution/`，主入口是 `index.json`。其中有 34 份去重后的完整许可文本、1 份必须保留的原始声明/Apple SDK 提示文件，以及版本、源码、目标依赖图、版权定位和 SHA256 记录。完整 crate 本体仍在原 `rust-crates/` 缓存，不重复复制。

这 23 项均未出现在本轮解析的 7 个 Python 扩展普通 `normal,build` 目标图中，也未出现在已有 renderer 两个目标图中。它们属于其他平台、可选功能或测试/基准依赖。**许可材料补齐并不表示这些候选已链接到冻结程序**，也不把它们作为普通 Linux/Windows 目标发行的新增阻断项。

## 目标图与可复核边界

从缓存的原始 sdist 解包到独立临时目录，逐一校验归档 SHA256；以本地 885 个 crate 构建离线 Cargo directory source。按各 `pyproject.toml` 中的 maturin `manifest-path` 和显式 `features`，保留默认特性，执行：

```text
cargo tree --offline --locked --manifest-path <sdist manifest>
  --target <x86_64-unknown-linux-gnu 或 x86_64-pc-windows-msvc>
  --edges normal,build --prefix none --format {p}|{l}
  --config source.crates-io.replace-with="license-vendor"
  --config source.license-vendor.directory="<本地解包 crate 目录>"
  [--features <原 pyproject 声明>]
```

`index.json` 和 `target-graphs/commands.json` 保存完整命令、原始输出及其哈希；`lockfiles/` 保存与缓存档案成员逐字一致的 8 份锁文件，包列表与原 `rust-locks.json` 完全一致。独立复核还比对了 7 个 sdist 的 39 个清单、锁文件、pyproject 原件，未见差异。

| 来源项目 | 固定版本 | 普通目标图 |
| --- | --- | --- |
| cryptography | 48.0.0 | Linux GNU、Windows MSVC |
| fastuuid | 0.14.0 | Linux GNU、Windows MSVC |
| hf-xet | 1.5.0 | Linux GNU、Windows MSVC |
| jiter | 0.15.0 | Linux GNU、Windows MSVC |
| pydantic-core | 2.46.4 | Linux GNU、Windows MSVC |
| rpds-py | 0.30.0 | Linux GNU、Windows MSVC |
| tokenizers | 0.23.1 | Linux GNU、Windows MSVC |
| native renderer | 原锁文件 | 复用并校验既有两份各 230 项的图 |

Windows 的新增图是在 **Linux 主机上解析 MSVC 目标**，主机构建依赖的 cfg 仍可能按 Linux 处理；没有执行 build script、原生 Windows 编译或链接。本轮也没有重建第三方 wheel，因此不声称已证明上游 wheel 的特性选择或最终链接集合。该限制记录在 `index.json.graph_limit`。这是对缓存源码的可复核依赖分类，不替代既有冻结文件来源清单。

## 23 个补全项

下表的许可表达式保留 crate 原文，不擅自改写斜杠、OR 或例外条款。每一行的完整原件路径和哈希见 `index.json.crates`；`dependency-edges.json` 保留来自 Cargo 清单的依赖种类、target cfg 和 optional 声明。

| crate / 版本 | 原声明 | 普通图外的原因 / 本轮材料 |
| --- | --- | --- |
| prost 0.12.6 | Apache-2.0 | hf_xet 的可选 profiling → pprof；补固定提交 LICENSE |
| symbolic-common 12.17.3 | MIT | 同一 profiling 路径，经 symbolic-demangle；补版本提交 LICENSE |
| tonic-prost 0.14.5 | MIT | 可选 tokio-console → console-subscriber → console-api；补 workspace LICENSE |
| valuable 0.1.1 | MIT | tracing 的可选依赖，并有 `cfg(tracing_unstable)` 条件；补 workspace LICENSE |
| jni-sys-macros 0.4.1 | MIT OR Apache-2.0 | Android JNI 路径；补两份完整原文 |
| rustls-platform-verifier-android 0.1.1 | MIT OR Apache-2.0 | Android 路径；补两份完整原文 |
| objc2-core-foundation 0.3.2 | Zlib OR Apache-2.0 OR MIT | Apple framework 路径；保留 LICENSE.md 和三份完整标准文本 |
| objc2-io-kit 0.3.2 | Zlib OR Apache-2.0 OR MIT | 同上 |
| objc2-system-configuration 0.3.2 | Zlib OR Apache-2.0 OR MIT | 同上 |
| wasip2 1.0.1+wasi-0.2.4 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | WASI 目标；补三份上游完整原文 |
| wasip2 1.0.2+wasi-0.2.9 | 同上 | WASI 目标；补该提交三份原文 |
| wasip3 0.4.0+wasi-0.3.0-rc-2026-01-06 | 同上 | WASI preview 3 目标；补该提交三份原文 |
| wasite 1.0.2 | Apache-2.0 OR BSL-1.0 OR MIT | whoami 的 WASI 路径；补 Apache、Boost、MIT 完整原文 |
| wasm-encoder 0.244.0 | Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | WIT/WASM 工具链候选；补三份完整原文 |
| wasmparser 0.244.0 | 同上 | 同上 |
| wit-component 0.244.0 | 同上 | 同上 |
| wit-parser 0.244.0 | 同上 | 同上 |
| wit-bindgen-rt 0.39.0 | 同上 | pydantic-core 锁文件中的 WASI 候选；补三份完整原文 |
| winapi-i686-pc-windows-gnu 0.4.0 | MIT/Apache-2.0 | GNU Windows import libraries，不属于 MSVC 目标；补原 MIT/Apache 文本 |
| winapi-x86_64-pc-windows-gnu 0.4.0 | MIT/Apache-2.0 | 同上 |
| anes 0.1.6 | MIT OR Apache-2.0 | jiter 的 benchmark/dev 路径；补两份原文 |
| codspeed 2.10.1 | MIT OR Apache-2.0 | jiter 的 benchmark/dev 路径；补两份原文 |
| codspeed-criterion-compat 2.10.1 | MIT OR Apache-2.0 | 同上 |

另外以 hf_xet 的 `profiling,tokio-console` 显式特性生成两个诊断图，确认 prost 0.12.6、symbolic-common 12.17.3、tonic-prost 0.14.5 出现；valuable 仍未启用。这两个图是说明可选依赖入口的证据，不是产品构建配置。

## 原文和版本如何绑定

19 个 crate 提供 `.cargo_vcs_info.json`。已用其中的 Git commit 读取上游 blob，保留 Git blob ID、固定提交 URL、原始字节与 SHA256；23 个 crate 的 `Cargo.toml.orig` 最终均与所选上游清单逐字一致。anes 的旧 VCS 信息未写 `path_in_vcs`，通过同一提交中的 `anes/Cargo.toml` 精确匹配定位。

4 个未携带 VCS 信息的归档采用如下证据，不猜测提交：

| 归档 | 固定上游证据 | 与 crate 的比较 |
| --- | --- | --- |
| symbolic-common 12.17.3 | [12.17.3 提交 7d9a706](https://github.com/getsentry/symbolic/tree/7d9a706e6345c77eba8a0562a7bf2e59efad4ee6/symbolic-common) | 清单、README 和 7 个源码文件共 9 项 Git blob 匹配 |
| rustls-platform-verifier-android 0.1.1 | [Android 0.1.1 版本提交 da3d9c3](https://github.com/rustls/rustls-platform-verifier/tree/da3d9c36f48fb9f8f97e94f132fdab67ab0fc75b/android-release-support) | 原始清单与 src/lib.rs 两项匹配；不声称重建归档内 AAR |
| 两个 winapi GNU 0.4.0 | [发布提交 9497609](https://github.com/retep998/winapi-rs/tree/9497609ef44cc9bcd16cd2411c0ee6ccaf5483aa) | 各自 Cargo.toml、build.rs、src/lib.rs 三项匹配；不声称重建 GNU import libraries |

重点原文可直接核对：[prost LICENSE](https://github.com/tokio-rs/prost/blob/d42c85e790263f78f6c626ceb0dac5fda0edcb41/LICENSE)、[symbolic LICENSE](https://github.com/getsentry/symbolic/blob/7d9a706e6345c77eba8a0562a7bf2e59efad4ee6/LICENSE)、[tonic LICENSE](https://github.com/hyperium/tonic/blob/21d24942a5a4a1806344beb331d4157d510a210c/LICENSE)、[valuable LICENSE](https://github.com/tokio-rs/valuable/blob/9efc29b6e58cef28f6566a47aa7e142a55fead77/LICENSE)。其余固定版本 URL 均记录于索引，并以 Git blob 哈希核验下载字节。

objc2 的 [原始 LICENSE.md](https://github.com/madsmtm/objc2/blob/7b1abfd750a2cacaea71d6a56ecfb83cb7de560b/LICENSE.md) 仅提供三种许可链接，并明确提示 Apple SDK 派生内容的问题。因此它被标为“声明与上游提示”，未计作完整文本；另附 [SPDX license-list-data 固定提交](https://github.com/spdx/license-list-data/tree/d46e94e2c78ceede1cfc63cfa0396472d2798d4c/text) 的 MIT、Apache-2.0、Zlib 全文。原有 Apple SDK 提示完整保留，本轮没有替上游裁定这项非目标平台问题。

完整许可证中的版权、年份和例外条款全部原样保存。`source-copyright-records.json` 另列出 30 个源码文件中的版权原文位置与文件哈希，完整上下文仍在对应 crate 本体中；没有把 Cargo authors 当成版权人，也没有补写上游未声明的年份。未在这 23 个 crate 本体及其固定提交的上游祖先目录发现额外独立 NOTICE/COPYING/COPYRIGHT 文件；检索树元数据亦已保存。

## 材料使用与校验

- `index.json`：组装入口。逐 crate 关联完整源码归档、声明、原文、目标分类和版本证据。
- `upstream/`：固定提交的原始许可证和清单；`standard-license-texts/`：objc2 声明所需的三份完整标准文本。
- `crate-declarations/`、`project-declarations/`、`lockfiles/`：可直接复核的原件。
- `target-graphs/`、`dependency-edges.json`：普通目标与可选特性的分类证据。
- `files.json`：证据有效载荷的 SHA256 清单（不含清单自身和随后生成的 ZIP/assembly 描述）；`verify.py`：离线验证本补充目录及已有缓存输入。
- `rust-license-supplement.zip`：包含上述有效载荷与 `files.json` 的许可/来源证据包。`assembly.json` 给出其 SHA256、23 份既有源码归档引用，以及目标图、研究记录和证据清单的哈希，供总装配机械读取。

在仓库内运行：

```bash
python3 packaging/desktop/build/source-cache/rust-license-resolution/verify.py
```

正式 assembly 可按实际目标清单引用已有 renderer supplement，以及本索引中需要随源码候选一并保留的原始许可文本。其他 agent 负责的 GCC、Qt、字体、Python runtime 材料不在本轮范围内。本记录完成的是这 23 个缺失独立许可文件候选的材料补齐与目标分类，不声称整包分发材料或干净系统验收已完成。
