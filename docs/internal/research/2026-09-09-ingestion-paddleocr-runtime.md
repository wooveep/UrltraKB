# PaddleOCR-VL-1.6 本地运行与离线分发约束

日期：2026-09-09。研究票：[研究：PaddleOCR-VL-1.6 本地运行与离线分发有哪些已验证约束？](https://github.com/wooveep/UrltraKB/issues/16)；继承总规格 [Spec: 大型 DOCX/PDF 可靠导入、PaddleOCR 与证据编译（三批设计）](https://github.com/wooveep/UrltraKB/issues/13) 与 ADR 0006 的产品边界。本报告完成的是公开证据调查，**不是安装、运行性能或离线验收**。本轮没有安装依赖、下载模型权重、执行 OCR/LLM、上传文档或修改业务代码。

## 结论与建议

可以提出一个足够具体、供下一张 HITL 决策选择的 CPU 验证候选：**标准 CPython 3.13.15（cp313，非自由线程构建）＋ `paddleocr[doc-parser]==3.7.0` ＋ `paddlex==3.7.2` ＋ `paddlepaddle==3.3.1`**，Windows x64 与 Linux x86_64 分别制作运行包；配套固定 revision 的 PP-DocLayoutV3 和 PaddleOCR-VL-1.6 模型包。所列 Python 补丁、技术组合及交付形式只是研究建议，尚未经用户最终选择或实机验证。版本依据见下节。

新增证据足以支持以下工程边界：

- **需要独立环境及进程。** PaddleX 3.7.2 要求 `PyYAML==6.0.2`，与本仓基线的 `pyyaml==6.0.3` 有真实冲突，不能直接作为普通依赖并入主应用。流水线还有不可据公开接口保证结束时限的工作线程，外部进程监督有必要性。[PaddleX 元数据](https://pypi.org/pypi/paddlex/3.7.2/json)、[OpenKB 基线](https://github.com/wooveep/UrltraKB/blob/b5cdc0d4574bf4b402b71b367fad22794bc2604e/pyproject.toml#L47)、[流水线收尾](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/paddleocr_vl/pipeline.py#L1157)
- **离线交付须覆盖解释器、依赖闭包、模型配置/分词器、字体及许可证。** 设置模型源检查开关不等于禁止联网；默认模型缓存也不是带摘要验证的交付清单。[模型解析/下载器](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/utils/official_models.py#L894)、[字体加载器](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/utils/fonts.py#L73)
- **完整流水线的 CPU 路径有官方支持及核心 wheel 证据；依赖闭包安装成功、最低 RAM、长文吞吐和取消上界均没有本轮实测证据。** 不以 VLM 文件大小、默认 batch 或当前研究机器配置推定这些指标。[VL 教程](https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/docs/version3.x/pipeline_usage/PaddleOCR-VL.md#L16)、[PaddlePaddle 发布元数据](https://pypi.org/pypi/paddlepaddle/3.3.1/json)

既定本地完整 VL-1.6、Windows/Linux CPU 目标、可选 GPU、显式云接口和 PageIndex Cloud 全部退役的选择不在本票重开。技术验证失败时记录失败事实；不能把远程推理客户端改称“完整本地引擎”来满足验收。

## 1. 证据分级与版本快照

本文以“官方声明”指文档写明的支持；“工件证据”指索引/manifest 中存在文件及发布方元数据；“源码证据”指固定提交可见的控制流；“建议/推断”指 OpenKB 候选交付边界；“待实测”指本轮没有运行验证。wheel 名称、发布哈希及模型 manifest 不证明目标机器上可安装或可用。

| 对象 | 2026-09-09 读取到的版本/固定点 | 可确认的范围 |
|---|---|---|
| PaddleOCR | PyPI 3.7.0，2026-06-11 上传；tag `v3.7.0` → `b03f46425e8ff4442b268ce449e3eef758146cd4` | `paddlex[ocr-core]>=3.7.0,<3.8.0`；`doc-parser` 增加 `paddlex[ocr,genai-client]` 同版本范围。不是完整闭包锁文件。[元数据](https://pypi.org/pypi/paddleocr/3.7.0/json)、[声明](https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/pyproject.toml#L49) |
| PaddleX | PyPI 3.7.2，2026-06-25 上传；tag `v3.7.2` → `ffb64904d23708863ff5b8da312a5cbd52a7f462` | 满足上述 PaddleOCR 版本范围；本文流水线/缓存源码均固定此提交。[元数据](https://pypi.org/pypi/paddlex/3.7.2/json)、[tag](https://github.com/PaddlePaddle/PaddleX/tree/v3.7.2) |
| PaddlePaddle CPU | PyPI 3.3.1，目标平台 wheel 于 2026-03-24/26 上传 | 有 CPython 3.9、3.10、3.11、3.12、3.13 的 `win_amd64`、`manylinux1_x86_64` wheel。[元数据](https://pypi.org/pypi/paddlepaddle/3.3.1/json) |
| PaddlePaddle GPU | 官方 cu118/cu126 索引均列出 3.3.1 的 Windows/Linux x64、cp311/cp313 wheel；PyPI 查询的最新版仍是 2.6.2 | GPU 3.x 不能靠默认 PyPI 最新版本选择；本轮只核对官方索引条目，未检查 wheel 内嵌 DLL/依赖。[cu126 索引](https://www.paddlepaddle.org.cn/packages/stable/cu126/paddlepaddle-gpu/)、[cu118 索引](https://www.paddlepaddle.org.cn/packages/stable/cu118/paddlepaddle-gpu/)、[PyPI GPU 元数据](https://pypi.org/pypi/paddlepaddle-gpu/2.6.2/json) |
| Python 候选 | CPython 3.13.15，2026-08-05 发布 | 官方提供 Windows x64 安装器/嵌入 ZIP 及源码包；Linux 完整可搬运解释器不是该页面已经提供并验证的工件。[发布页](https://www.python.org/downloads/release/python-31315/) |
| VL 教程 | PaddleOCR `release/3.7` 读取点 `cdc6d02f680f922b2f8c3605f716d3022a9c9a0f` | 本文引用的 VL 教程和 Python API 文件与 3.7.0 tag 一致；其后差异不包含这两个文件。[比较](https://github.com/PaddlePaddle/PaddleOCR/compare/b03f46425e8ff4442b268ce449e3eef758146cd4...cdc6d02f680f922b2f8c3605f716d3022a9c9a0f) |

PyPI 声明的 PaddleOCR/PaddleX `Requires-Python >=3.8` 不能覆盖流水线实际支持条件。VL 教程写明验证 Python **3.9–3.13**、PaddlePaddle **≥3.2.1**，并要求 CPU 与 GPU 版不能同环境共装。本文选 3.13.15 是减少首轮矩阵的候选取舍；3.14、自由线程 cp313t、PyPy 不在本轮可证明范围内。[安装教程](https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/docs/version3.x/pipeline_usage/PaddleOCR-VL.md#L155)

## 2. 候选运行包及支持矩阵

所有行均未实测；“存在”只表示发布方索引列出了对应工件。CPU 行共用 `paddleocr[doc-parser]==3.7.0`、`paddlex==3.7.2` 和本报告固定模型 revision。

| 候选 | 精确核心组合 | 官方支持/工件证据 | 尚需补齐 |
|---|---|---|---|
| Windows x64 CPU，首要 | CPython 3.13.15；`paddlepaddle==3.3.1`；`device=cpu`、native | VL 教程支持 x64 CPU 完整原生流程；`paddlepaddle-3.3.1-cp313-cp313-win_amd64.whl` 存在 | Windows 具体版本、AVX/MKL、运行库、全部依赖 DLL、中文/空格/长路径、重装/迁移、完整流程实测 |
| Linux x86_64 CPU，首要 | 同上，Linux CPU wheel | `paddlepaddle-3.3.1-cp313-cp313-manylinux1_x86_64.whl` 在 PyPI；官方 CPU 索引另列 `linux_x86_64` 文件 | 选定发行版、glibc/系统动态库、完整依赖 wheel 闭包；不能从某一个 manylinux 标签推定整个应用的系统下限 |
| Windows x64 NVIDIA GPU，可选 | 同 Python/OCR/X；换成 `paddlepaddle-gpu==3.3.1`，cu126 工件；单独环境 | 官方 cu126 索引存在 `paddlepaddle_gpu-3.3.1-cp313-cp313-win_amd64.whl`；VL 有 native GPU 路径 | 选定 GPU/驱动、CUDA 运行库/DLL、VRAM、吞吐及与 CPU 变体隔离；不支持以 vLLM/SGLang/FastDeploy 原生 Windows 服务代替 |
| Linux x86_64 NVIDIA GPU，可选 | 同上，cu126 Linux GPU wheel | 索引存在 `paddlepaddle_gpu-3.3.1-cp313-cp313-linux_x86_64.whl` | 同上；轮子内嵌运行库及再分发清单待工件审计 |

矩阵依据：[VL 支持矩阵及环境要求](https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/docs/version3.x/pipeline_usage/PaddleOCR-VL.md#L40)、[CPU PyPI](https://pypi.org/pypi/paddlepaddle/3.3.1/json)、[官方 CPU 索引](https://www.paddlepaddle.org.cn/packages/stable/cpu/paddlepaddle/)、[官方 cu126 索引](https://www.paddlepaddle.org.cn/packages/stable/cu126/paddlepaddle-gpu/)。

平台条件不能拼成一个未经验证的承诺：PaddlePaddle 3.3 安装文档写明 64 位 x86_64、pip ≥20.2.2、默认 AVX/MKL，且不再提供 noavx 包。Linux 文档示例仍为 3.3.0，Windows GPU 示例仍为 3.2.2；VL 教程原生 GPU 条件是 CC≥7.0、CUDA≥11.8，而框架安装文档写 GPU 算力 “over 7.5”。因此本票不判定一张边界 GPU 一定可用，更不把新索引中的 3.3.1 当作四组件联合认证。[Linux 固定文档](https://github.com/PaddlePaddle/docs/blob/5d295eba735d9fc0832dd1f4682b35f2ce5c8db0/docs/install/pip/linux-pip_en.md)、[Windows 固定文档](https://github.com/PaddlePaddle/docs/blob/5d295eba735d9fc0832dd1f4682b35f2ce5c8db0/docs/install/pip/windows-pip_en.md)、[VL 环境要求](https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/docs/version3.x/pipeline_usage/PaddleOCR-VL.md#L76)

### 完整本地与服务客户端的界线

VL 完整流程是版面分析 → 裁块 → VLM → 按阅读顺序组装。官方明确指出单独调用 Transformers VLM 或请求 vLLM/SGLang/FastDeploy 不等于完整流程。native 模式须让版面与 VLM 都在本机执行；`paddlex[genai-client]` 作为依赖被安装，也不意味着已选择远程后端。[流程定义](https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/docs/version3.x/pipeline_usage/PaddleOCR-VL.md#L16)、[native 默认配置](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/configs/pipelines/PaddleOCR-VL-1.6.yaml)、[本地/远程 predictor](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/models/doc_vlm/predictor.py#L47)

vLLM/SGLang/FastDeploy 的现有路径有 GPU/CUDA 要求，且官方明确不支持原生 Windows；混合引擎通常还要分环境。它们不应成为 CPU 首包依赖。本轮不穷尽这些后端组合。官方提供约 10 GB 的 native NVIDIA 离线镜像，并说明先联网拉取、导出后移入离线机器；这是 GPU 交付参考，不能证明有现成 Windows/Linux CPU 便携包。镜像 `latest` 可变，若后来选容器交付仍须固定 digest，本轮没有拉镜像或核验 digest。[后端限制与离线镜像](https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/docs/version3.x/pipeline_usage/PaddleOCR-VL.md#L69)

### 已取得的 CPU 核心工件摘要

以下是发布方 JSON 中的 SHA-256，**没有下载文件做本地重算**；不能替代余下依赖的校验。

| 文件 | 字节数 | 发布方 SHA-256 |
|---|---:|---|
| `paddleocr-3.7.0-py3-none-any.whl` | 146750 | `c0f0a81ad4112727f30c6fcf986ac0ef6a120d31ee0991a01fae0357ee32d338` |
| `paddlex-3.7.2-py3-none-any.whl` | 2239708 | `f1678bf650bbaccfd8f0d4e49d0ae631b4685c829fdae6e802ccd90d4fcb9a7f` |
| `paddlepaddle-3.3.1-cp313-cp313-win_amd64.whl` | 104794530 | `1203e2e1114b49e73a8440b68837ce5c93fdab51fe4838c5e5874ab40f58f747` |
| `paddlepaddle-3.3.1-cp313-cp313-manylinux1_x86_64.whl` | 194837642 | `4f28038427649bb2fcfd4d52efa85ed8311df19122b4ebedc942484a0b57b032` |

来源：[OCR 3.7.0](https://pypi.org/pypi/paddleocr/3.7.0/json)、[X 3.7.2](https://pypi.org/pypi/paddlex/3.7.2/json)、[Paddle 3.3.1](https://pypi.org/pypi/paddlepaddle/3.3.1/json)。上述记录均未标 yanked。不要把 PyPI `manylinux1_x86_64` 工件的摘要套到官方索引同版本 `linux_x86_64` 工件；文件名/来源不同就需要分别登记。当前读取的 cu126/cu118 索引链接不带 SHA-256 fragment，GPU 摘要仍待取得。

## 3. 依赖闭包：已知约束和未完成部分

核心组合满足上游声明的 PaddleX 范围与 PaddlePaddle 最低版本，**不代表解析器已经证明整个依赖集合可解，也不代表二进制 ABI/DLL 加载成功**。当前没有生成、安装或测试 Windows/Linux 完整锁文件。

| 层次 | 从发布元数据读到的直接要求（按用途列举） | 对运行包的影响 |
|---|---|---|
| PaddleOCR 3.7.0 + doc-parser | `paddlex[ocr-core,ocr,genai-client]>=3.7.0,<3.8.0`，`PyYAML>=6`、requests、`aiohttp>=3.8.0`、`typing-extensions>=4.12` | 避免 `pip install -U` 作为交付策略；3.7.2 是本报告候选 pin |
| PaddleX 3.7.2 基础 | `PyYAML==6.0.2`、`numpy>=1.24,<2.4`、`pydantic>=2`、`pandas>=1.3`；aistudio-sdk、huggingface-hub、modelscope、filelock、Pillow、requests、ruamel.yaml 等 | 模型下载客户端仍属于基础依赖；安装它们不等于获得离线资产。与主应用 PyYAML pin 冲突 |
| OCR/native 模型相关 extras | `opencv-contrib-python==4.10.0.84`、`pypdfium2>=4`、`safetensors>=0.7.0`、`tokenizers>=0.19`；sentencepiece、tiktoken、einops、Jinja2、ftfy、regex、python-bidi、shapely、pyclipper、scipy、scikit-learn、lxml 等 | 包括原生扩展、PDFium、OpenCV；不能只收三个 Paddle wheel。所有范围依赖仍需逐平台固定版本、工件和哈希 |
| genai-client extra | `openai>=1.63` | 在独立包保留上游要求；不因此绑定 OpenKB 主环境的 OpenAI SDK |
| PaddlePaddle 3.3.1 CPU | httpx、`numpy>=1.21`、`protobuf>=3.20.2`、Pillow、`opt_einsum==3.3.0`、networkx、typing_extensions、`safetensors>=0.6.0`；Python≥3.12 时 setuptools | Python 3.13 的 setuptools 也须进入闭包；NumPy 与 safetensors 要求取各层交集 |

表格是**直接约束摘录，不是可执行 requirements 文件或完整依赖清单**。全部字段可在版本化 [PaddleOCR](https://pypi.org/pypi/paddleocr/3.7.0/json)、[PaddleX](https://pypi.org/pypi/paddlex/3.7.2/json)、[PaddlePaddle](https://pypi.org/pypi/paddlepaddle/3.3.1/json) `requires_dist` 核对。Native 实现由 PaddleX 本地建模代码加载，不需仅因模型 `config.json` 中记录 `transformers_version` 就安装该版本 Transformers。[模型加载](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/models/doc_vlm/predictor.py#L100)

建议后续在授权的构建环境形成按 OS/架构/ABI/CPU或CUDA变体分开的 wheelhouse，所有传递依赖精确锁定、带来源和 SHA-256；只从本地清单安装，缺件即失败。pip 官方支持固定版本、哈希校验及 wheelhouse 离线安装，也提示二进制 wheelhouse 通常不能跨 OS/架构通用。[可重复安装说明](https://pip.pypa.io/en/stable/topics/repeatable-installs/)

## 4. 模型与附属资产清单

PaddleX 3.7.2 的 `PaddleOCR-VL-1.6.yaml` 明确指定 `PP-DocLayoutV3` 和 `PaddleOCR-VL-1.6-0.9B`；下载器将 VL 名称中的 `-0.9B` 去掉，实际对应第一方 HF 仓库 `PaddlePaddle/PaddleOCR-VL-1.6`。预处理默认关闭，方向分类和 UVDoc 的子配置默认开启但只有预处理被启用才会构建，须在交付配置中明确每个开关。[配置](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/configs/pipelines/PaddleOCR-VL-1.6.yaml)、[名称映射](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/utils/official_models.py#L929)、[按开关构建](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/paddleocr_vl/pipeline.py#L97)

以下取自第一方 HF `api/models/...?...blobs=true` 返回的 `sha`、`siblings`、`size`、LFS SHA-256；仅读取 manifest 和小型配置/许可证/模型卡，不读取权重。

| 组件及角色 | 固定模型仓库 revision | 文件与发布元数据 |
|---|---|---|
| PP-DocLayoutV3，完整流程必需 | `7b48a7566925fa464281f930c58eee04fe2c862a` | `inference.json` 1196890 B；`inference.pdiparams` 130806572 B；`inference.yml` 1482 B；另有 README/.gitattributes。权重 SHA-256 `70bd316b0582769ec968829fd1feb1a6a58b7c941b938327e551b6b12b45c137`。[固定树](https://huggingface.co/PaddlePaddle/PP-DocLayoutV3/tree/7b48a7566925fa464281f930c58eee04fe2c862a) |
| PaddleOCR-VL-1.6，完整流程必需 | `c5630abae1d940eafe0697512a0325494b02ab42` | `model.safetensors` 1917255968 B，SHA-256 `85a479d506a11e724e7285d395c551be69f41dbc16b6342d3cacfb189aed71db`；分词器、配置等见下文。[固定树](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6/tree/c5630abae1d940eafe0697512a0325494b02ab42) |
| PP-LCNet_x1_0_doc_ori，启用方向分类才需要 | `d3b95a6dff5fe8a94f2748e12b61cb26818a0df8` | `inference.json`、`inference.pdiparams`、`inference.yml`、`config.json` 与模型卡；权重 6754166 B，SHA-256 `e8d6e7c5d264507e40e58a655779059d616b20d7441ea22047d829eb3931989c`。[固定树](https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori/tree/d3b95a6dff5fe8a94f2748e12b61cb26818a0df8) |
| UVDoc，启用去畸变才需要 | `16c3f0ea9c2f0c6a57e24160f7eeaa7574613fa3` | `inference.json`、`inference.pdiparams`、`inference.yml`、`config.json` 与模型卡；权重 32054311 B，SHA-256 `810488899520e0da843b9bd9769ba4949f1c81e357f0eceb12d4a7da459c3eca`。[固定树](https://huggingface.co/PaddlePaddle/UVDoc/tree/16c3f0ea9c2f0c6a57e24160f7eeaa7574613fa3) |

VL-1.6 完整仓库文件集合还包括：`config.json`、`generation_config.json`、`inference.yml`、`preprocessor_config.json`、`processor_config.json`、`tokenizer.model`、`tokenizer.json`、`tokenizer_config.json`、`added_tokens.json`、`special_tokens_map.json`、`chat_template.jinja`；自定义实现 `configuration_paddleocr_vl.py`、`modeling_paddleocr_vl.py`、`image_processing_paddleocr_vl.py`、`processing_paddleocr_vl.py`；以及 `LICENSE`、`README.md`、`.gitattributes`、`.eval_results/real5_omnidocbench.yaml`。模型 API 名称中的 0.9B 不是可省略版面模型的许可证或功能变体。[固定 manifest API](https://huggingface.co/api/models/PaddlePaddle/PaddleOCR-VL-1.6/revision/c5630abae1d940eafe0697512a0325494b02ab42?blobs=true)

源码直接从本地目录读取 `tokenizer.model`、`chat_template.jinja` 和处理器配置，并以 `convert_from_hf=True` 加载模型。**首轮建议完整保留固定模型仓库资产，再通过验证确定可裁剪项；本轮没有证明删掉某些 Python/JSON 文件后仍能完整运行。** 数据配置里记录 `torch_dtype=bfloat16`，但 native predictor 的 CPU 路径选择 `float32`；磁盘权重 1.917 GB 不能当 RAM 需求。[分词器构建](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/models/doc_vlm/predictor.py#L311)、[native dtype](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/models/doc_vlm/predictor.py#L63)、[CPU 不走 BF16 判定](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/utils/misc.py#L27)

按发布 manifest 将全部列出文件字节数相加，VL-1.6 为 **1930462423 B**，版面仓库 **132018596 B**，方向模型 **6870359 B**，UVDoc **32253154 B**；两个核心仓库合计 **2062481019 B**，四个仓库合计 **2101604532 B**。这是元数据求和，包含说明文件、不含模型托管平台缓存开销；不是压缩下载体积、最终安装空间、峰值内存或实测值。除权重和少数 LFS 文件外，HF `blobId` 是 Git blob 标识，**不能写成 SHA-256**；发布打包仍须逐文件计算校验值。来源为上表固定模型树及 [HF 模型 API](https://huggingface.co/api/models/PaddlePaddle/PaddleOCR-VL-1.6/revision/c5630abae1d940eafe0697512a0325494b02ab42?blobs=true)。

## 5. 离线加载、缓存和交付边界

### 可依赖的公开入口

PaddleOCR Python/CLI 提供 `layout_detection_model_dir`、`vl_rec_model_dir`、`doc_orientation_classify_model_dir`、`doc_unwarping_model_dir`；应把所有启用的模型指向已验证本地目录，同时固定 `pipeline_version="v1.6"`、`vl_rec_backend="native"` 和 `device="cpu"`（或显式 GPU）。默认设备不能代替设备预检；本地选项不得填写推理服务 URL。[参数定义及配置映射](https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/paddleocr/_pipelines/paddleocr_vl.py#L38)

| 路径/开关 | 源码事实 | 离线交付要求（建议） |
|---|---|---|
| `PADDLE_PDX_CACHE_HOME` | 默认是用户目录下 `.paddlex`；其中有 `official_models`、`func_ret`、`locks`、`temp` 等 | 进程启动前指定受管理且可写的独立缓存；模型本体与可写缓存分开管理。[缓存源码](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/utils/cache.py#L28) |
| `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK` | 只跳过模型托管源连通性预检查；仍构造托管源候选，缺模型时仍可能下载 | 不能命名为“离线模式已生效”；缺件须由包校验提前失败，断网试验验证没有请求。[源码](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/utils/official_models.py#L894) |
| `PADDLE_PDX_MODEL_SOURCE` | 改变优先托管源；还有其他源回退；HF `snapshot_download` 调用没有固定 revision 参数 | 不以默认在线首次运行建立发布版本；构建阶段按 manifest 固定 revision，并显式本地目录加载。[HF 下载](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/utils/official_models.py#L773)、[回退](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/utils/official_models.py#L987) |
| 模型缓存命中 | 下载器先判断候选目录存在，存在即复用；有跨进程下载文件锁，但这段命中逻辑没有逐文件哈希校验 | 缺件、损坏或错误 revision 不能靠“目录存在”放行；包 manifest 自行验证。不能将下载锁描述为内容完整性保证。[缓存命中](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/utils/official_models.py#L929) |
| 字体 | 按需访问 `Font.path` 时从 BOS 下载，缓存至 `fonts`；已存在的 `PADDLE_PDX_LOCAL_FONT_FILE_PATH` 可覆盖默认字体路径 | 渲染属于必须纳入断网验证的功能；选可再分发字体并测试语言覆盖，不直接假定名为 PingFang/仿宋的下载项可以随应用分发。[字体源码](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/utils/fonts.py#L73) |
| URL 输入 | 图片/PDF URL 会下载到 `predict_input`；文件夹输入会递归枚举支持的文件 | 离线 worker 只接收受管理本地输入及显式页清单；不能让外部 URL 绕过离线策略。[输入 sampler](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/common/batch_sampler/image_batch_sampler.py#L74) |

### 可选包交付方式

建议下一步优先验证“按平台的解释器/离线安装材料＋固定 wheelhouse＋独立 worker＋共用模型资产包”，在目标目录创建受管理环境。每个包都带版本、目标平台、解释器 ABI、依赖/模型/字体清单和校验摘要。安装、重装与升级应验证后切换活动版本，避免覆盖正在执行的环境；这些是候选交付边界，尚未实现。

不能把开发机 `.venv` 压缩后视为通用便携包：Python 官方明确 venv 不应被搬运/复制，应该在目标位置重建。Windows 嵌入 ZIP 是另一个候选，但默认没有 pip，不支持像普通 Python 那样用 pip 管依赖，且应用安装器负责所需 C Runtime；如果选它，第三方包要作为应用的受管资产交付。Linux 的解释器发行形态与基础系统依赖仍需选定、构建和验证。[venv 官方说明](https://docs.python.org/3.13/library/venv.html)、[Windows 嵌入分发](https://docs.python.org/3.13/using/windows.html#the-embeddable-package)

**离线交付清单：**（1）解释器、安装器及系统运行库；（2）逐平台固定依赖闭包和全部 wheel；（3）本节所有启用模型的完整固定目录；（4）明确的 pipeline YAML/参数快照；（5）本地字体、分词器与模型附属文件；（6）每文件字节数/哈希、来源 URL/revision 和包总摘要；（7）第三方 LICENSE/NOTICE/SBOM；（8）独立可写缓存与受管理输出目录约定；（9）缺件/错设备/不兼容/空间不足的诊断与重装流程；（10）断网安装、运行及回收验证记录。清单是后续发布要求，现有公开资料不构成已经做出的离线安装包。

## 6. 长文分片、内存与停止

### 有依据的风险

- PaddleX VL-1.6 配置的页级 `batch_size=64`、版面 batch=8、`use_queues=True`。队列分支内部允许 64 个 batch 在途；原生 VLM 的常量 batch 却是 1。**逐页产出 generator 不等于只保留一页内存**；这些是不同层次的并行/批次参数。[配置](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/configs/pipelines/PaddleOCR-VL-1.6.yaml)、[队列](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/paddleocr_vl/pipeline.py#L1018)、[VLM 常量](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/models/doc_vlm/constants.py#L37)
- PDF 输入会遍历页并渲染；`page_index` 在 sampler 中来自从 0 开始的 `enumerate`。默认 PDF 渲染 scale 为 2.0，源码像素上限为 178956970，超限有缩放逻辑。该数值不是 OpenKB 可承诺的内存安全上限，渲染、CV/VLM、输出仍有其他分配。直接提交裁出的图片时，Paddle 返回的 `page_index` 可能为 None，原文物理页映射必须由 OpenKB 保留。[sampler](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/common/batch_sampler/image_batch_sampler.py#L110)、[PDF 渲染](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/utils/pdf_rendering.py#L20)、[scale](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/utils/flags.py#L97)
- 公开预测接口有 `max_new_tokens`、`min_pixels`、`max_pixels`、`use_queues` 等参数；检查到的接口没有停止 token 或强制执行总时限契约。原生 VLM 默认生成上限 8192 是代码常量，不是整份资料的 token/耗时预算。[API](https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/paddleocr/_pipelines/paddleocr_vl.py#L101)、[常量](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/models/doc_vlm/constants.py#L37)
- queue 模式建立三个 `daemon=False` 线程；`finally` 设置 shutdown 事件，分别 `join(timeout=5)`，线程仍活只记 warning；队列写入有无超时的 `put`。`close()` 转交 VLM 关闭，不能据此推定当前 native 运算已被中止。因此**5 秒不是停止保证，三个 join 的时长也不能变成可承诺的退出上界**。[线程与收尾](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/paddleocr_vl/pipeline.py#L1018)、[close](https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/paddleocr_vl/pipeline.py#L186)

### 候选交付边界（推断，待实测）

第一轮从显式小页批、`use_queues=False` 和原生 VLM batch=1 验证资源边界，再校准吞吐；小 batch 不自动解决单张超大页面或 VLM 卡住。OpenKB 在资料解析层建立页范围/字节数/像素数清单，将输入拆成可恢复片段，逐片验证块、原页号和必需图片资产。页 batch、页分片、VLM token 上限分别记录，不能共用一个“批量大小”概念。

worker 加载一套固定运行包与模型，接收本地受管理输入，仅把候选解析产物写至暂存区；主进程拥有预算、停止、输入/资产验证与正式提交权。先请求正常收尾，再根据监督时限终止整个 worker 执行范围并确认退出，之后才报告停止、清理或重试。常驻 worker 可降低重复模型加载成本，但须证明失败后状态可重置；每片新进程便于隔离但会重复加载。二者的启动成本/残留内存没有公开保证，应由首轮实测选择。

跨页表格与标题重建需要后续 `restructure_pages`，官方示例会收集 `pages_res = list(output)`。分片不能默默破坏跨页关系，也不能为重建再把全部页图像无界留内存；保留按片页结果，验证相邻片段连接及原页映射，再确定重建范围。[官方多页示例](https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/docs/version3.x/pipeline_usage/PaddleOCR-VL.md#L675)

## 7. 许可证与再分发证据

| 资产 | 已核对的证据 | 交付时仍须满足/核验 |
|---|---|---|
| PaddleOCR / PaddleX / PaddlePaddle | 发布元数据分别标记 Apache 2.0 / Apache-2.0 / Apache Software License；官方源码带 Apache-2.0 声明。[OCR](https://pypi.org/pypi/paddleocr/3.7.0/json)、[X](https://pypi.org/pypi/paddlex/3.7.2/json)、[Paddle](https://pypi.org/pypi/paddlepaddle/3.3.1/json) | 每个实际分发 wheel 的 LICENSE/NOTICE 与第三方原生库清单；根项目许可证不能覆盖全部依赖 |
| VL-1.6 权重/模型目录 | 第一方固定模型卡标 Apache-2.0，目录有完整 LICENSE。[LICENSE](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6/blob/c5630abae1d940eafe0697512a0325494b02ab42/LICENSE) | 随包保留 LICENSE、出处、原声明及修改说明；不能只交权重不交许可 |
| PP-DocLayoutV3 / 方向模型 / UVDoc | 第一方固定模型卡均标 `license: apache-2.0`；本轮 manifest 未见独立 LICENSE 文件。[版面卡](https://huggingface.co/PaddlePaddle/PP-DocLayoutV3/blob/7b48a7566925fa464281f930c58eee04fe2c862a/README.md)、[方向卡](https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori/blob/d3b95a6dff5fe8a94f2748e12b61cb26818a0df8/README.md)、[UVDoc 卡](https://huggingface.co/PaddlePaddle/UVDoc/blob/16c3f0ea9c2f0c6a57e24160f7eeaa7574613fa3/README.md) | 保留模型卡、发行出处与所声明许可证文本，实际分发审计记录此证据粒度；没有调查出全链路训练数据/第三方权重权利证明 |
| 字体及 CUDA/底层库/解释器 | 字体源码仅给下载地址/名称，不能从这个文件推定字体再分发授权；GPU wheel 内容及传递依赖未下载审计 | 固定实际选用字体并携带其许可证；对解释器、PDFium、OpenCV、BLAS/CUDA 等按最终工件形成 LICENSE/NOTICE/SBOM；本轮不声称“整个离线包全是 Apache-2.0” |

Apache-2.0 第 4 节要求随分发提供许可证、标记修改、保留相关声明并处理上游 NOTICE；第 6 节不自动授予商标使用权。这些是官方条文要求，不能代替逐工件许可盘点。[Apache 官方许可证](https://www.apache.org/licenses/LICENSE-2.0)

## 8. 最小跨平台验证门槛与下一张 HITL 决策

以下全部是**拟议的后续授权范围，当前未执行**。建议先选择两个 CPU 目标的具名 OS 版本、解释器来源、核心组合和样本规模，再授权构建/安装/权重获取及本地实测；GPU 可在 CPU 包通过后追加。不能将研究票完成解释成这些动作已经获授权。

| 门槛 | Windows x64 CPU | Linux x86_64 CPU | 通过证据 |
|---|---|---|---|
| 清洁安装与依赖闭包 | 无既有 Paddle/Python 包干扰的目标环境；核验 C Runtime/DLL | 具名发行版和 glibc/动态库环境 | 每个 wheel/解释器来源、版本/哈希，完整锁文件，离线安装记录、依赖一致性检查及 Paddle 自检；不能只记录 `import paddle` |
| 完整 native 流程 | 同一可人工核对的本地扫描样本，显式 CPU | 同左 | 版面模型与 VL 都从指定本地目录加载；内容块、阅读顺序、页映射、表格/图片引用正确，无远程 VLM |
| 真正离线 | 空用户模型缓存，阻断外网；模型目录完整/缺件/损坏三种状态；含渲染导出 | 同左 | 完整包无网络请求且成功；缺件/损坏清晰失败，无隐式下载或云回退；字体无需联网 |
| 资源和长文 | 原生/扫描/混合多页、大页面、跨页表格；测试 Intel/AMD 代表设备 | 同左 | 记录冷/热启动、每页及整片耗时、峰值 RSS/提交内存、临时磁盘、模型常驻开销；调整 batch/像素后同时检查质量 |
| 停止与故障 | 渲染、版面、VLM、队列积压、输出保存时停止；进程退出确认 | 同左 | 停止后没有活动 worker/迟到产物提交；超时可回收；缺内存/崩溃/损坏输入可诊断，可按已验证片段恢复 |
| 重复安装/路径 | 中文、空格、长路径；用户目录变化；同版本重装、升级失败 | 权限受限、只读模型/可写缓存、路径变化；同版本重装 | 未破坏主应用环境和既有模型；版本切换可识别，错误环境/ABI/哈希拒绝，运行中的旧环境不被覆盖 |
| 交付许可 | 逐工件许可与模型卡、字体授权、SBOM | 同左 | 清单闭合，发布工件对应清单；许可证/NOTICE 随包可读 |

最低 RAM、每页时延、首次启动上界、可中止时限、整包安装空间、最大安全分片页数和跨页重建内存，当前全部保持未知。可以把若干候选资源档位列入后续测试，但不能把它们写成最低配置；必须记录 CPU 型号/指令集、内存、线程设置、文档像素与内容、完整版本、缓存冷热状态和成功/失败范围。

供下一张 HITL 决策的建议是：**接受“独立受管 CPU 运行包＋固定共用模型资产＋受监督 worker”作为首轮验证方向；选择本报告核心组合为试验候选，允许结果驱动调整；将依赖闭包、离线和停止门槛作为发布阻断项。** 另一个可选交付形态是 Windows 嵌入式自包含包及 Linux 自包含解释器包，但它新增运行库/可搬运性验证，不能通过压缩 venv 宣称已经完成。两个形态均保留完整本地 VL-1.6 及 Windows/Linux CPU 目标，不替换既定产品选择。

## 9. 调查记录与限制

使用 `research` 工作流，由后台研究代理独立完成；网络读取遵循 `agent-reach` GitHub/gh 路由，并读取第一方 PyPI、Paddle wheel 索引、HF 模型发布元数据和官方文档。只保存这一份报告到研究分支，原始临时文本与下载清单未作为发布工件提交。调查读取的本仓基线为 `b5cdc0d4574bf4b402b71b367fad22794bc2604e`；定位本地依赖前先使用现有 CodeGraph，没有创建索引。

动态索引和模型主分支会变化，因此报告尽量使用源码提交、模型 revision 和版本化元数据。公开 manifest/哈希属于发布方声明，本轮未以实际下载校验。没有完整检查所有 GPU 后端、所有 Linux 发行版或所有 Windows 版本，也没有把普通 API 文档、wheel 可见性或某台开发机配置冒充联合兼容性验证。
