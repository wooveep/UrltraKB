# DeepSeek 图内描述：锁定运行时的传输契约研究

- 调查日期：2026-10-08；开发基线：`be5aad49cffac16d36e4c676f099467a5ab3cc22`。
- 对应研究：[研究：DeepSeek 图片理解怎样接入当前锁定的 LLM 运行时？](https://github.com/wooveep/UrltraKB/issues/128)。
- 产品前提：[PDF 识别决策地图：云端 OCR、图内描述与可恢复导入](https://github.com/wooveep/UrltraKB/issues/126)。
- 范围：官方协议、固定依赖源码、离线录制端点；无真实 DeepSeek／OCR 调用、文档上传、依赖安装或生产修改。

## 结论

1. 官方现行图片模型是 `deepseek-flash`。当前本地失败来自旧适配逻辑，不能据此推断在线模型不支持图片。[模型表][models]
2. 锁定 LiteLLM 的 DeepSeek 适配器无条件把含文字的内容数组压成字符串，直接 user 图文与工具图文都受影响。仅改 `supports_vision` 不能修复。[变换源码][deepseek-transform]；下文离线矩阵。
3. 当前 Agents SDK 已把直接 user 图片转换成 Chat Completions 图片块；它并非这条路径最初丢图处。工具图片也被保留到 LiteLLM，但两种输入需分别验收。[SDK 调用][sdk-litellm]、[SDK 转换][sdk-converter]
4. 最小候选是审阅并修复已内置的 DeepSeek 适配器，再经共享执行器做一次直接图文调用；仅更改导入提示词或元数据不足以交付。此处是研究建议，尚未决定修复方案、升级依赖或实现新角色。
5. 首版图内描述无需为了获取图片而引入工具循环。真正需要 Responses 的工具图片、文件引用或 schema 能力时，必须同时接入现有配置、预算和用量边界。[运行时][executor]、[Responses 契约][responses]

## 证据基线

| 层级 | 已核对内容 | 能证明的范围 |
| --- | --- | --- |
| 官方文档 | DeepSeek Vision、Chat／Responses、Files、Thinking、JSON、Usage | 2026-10-08 公布的服务契约；不是本账户实测 |
| 固定依赖源码 | 仓库内置 LiteLLM、Agents 0.17.3、OpenAI 2.44.0 | 实际转换、参数过滤和客户端调用路径 |
| 离线执行 | 16 个 localhost HTTP 请求、1 个文件引用解析失败、10 个现有测试用例 | 请求在客户端实际送出的内容与本地运行时行为 |
| 待实测 | 在线接受度、识别质量、延迟、费用、关闭连接后的服务行为 | 本轮没有作通过声明 |

`pyproject.toml` 与 `uv.lock` 固定 `litellm==1.87.2+urltrakb.3`、`openai-agents==0.17.3`、`openai==2.44.0`；实际复用虚拟环境也为这三个版本，HTTPX 为 `0.28.1`。`PYTHONPATH` 强制加载本研究工作树的 OpenKB 与内置 LiteLLM。[项目依赖][deps]

Agents 0.17.3 标签解析到提交 `17f7caeaa33d97eee7152f9834af5e706b8f90e2`。通过 `gh api` 获取下列两文件，与实际安装文件逐字节 SHA-256 一致：

| 文件 | SHA-256 |
| --- | --- |
| `src/agents/models/chatcmpl_converter.py` | `93d61e0fe59fe0ca12317df3c80ba7d94d1e0bd30ab849770ee2314b7632a33e` |
| `src/agents/extensions/models/litellm_model.py` | `c7991711017a09717d6e2b6e31dab28ed3bb94a93167d067985fab9cc924531a` |

本地能力表版本为 `litellm-1.87.2-1296275dc52d`，明确 `prices_are_live=false`；缺少 `deepseek-flash` 及其带 provider 前缀条目。实际 `supports_vision("deepseek/deepseek-flash")` 返回 False；旧 `deepseek-chat` 条目也没有 vision 标记。不能把该表当成现行服务能力或价格。[表身份][table-id]、[固定能力表][table]

## 官方图片输入边界

| 项目 | 已公布契约 | 首版实现含义 |
| --- | --- | --- |
| 型号 | `deepseek-flash` 对应 DeepSeek-V4.1-Flash；`deepseek-v4-pro` 不支持 vision | 保留 DeepSeek 型号与端点的明确绑定。[模型表][models] |
| 模型别名 | `deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` 已退役，旧名称仍转到新 Flash | 固定字符串不能保证固定历史模型权重；记录处理版本与当时策略。[模型表][models] |
| Chat | `POST https://api.deepseek.com/chat/completions`；user `content` 中用 `text` 与 `image_url:{url,detail}` | 导入直接 user 图文属于官方最明确路径。[Chat API][chat] |
| Responses | `POST https://api.deepseek.com/responses`；图片用 `input_image`，`image_url` 或 `file_id` 二选一 | 并非把现有 `LitellmModel` 的输入形状叫 Responses 就会走这个端点。[Responses API][responses-api] |
| 图片来源 | Base64 data URL、可公开访问的 HTTP(S) URL、Files API 引用；JPEG／PNG／GIF／WebP | 本地图来源为本地裁剪，Base64 可避免额外远程文件生命周期。[Vision][vision] |
| detail | `low` 缩至 512×512；`high`、`original`、当前 `auto` 保留原图输入 | 后续推理仍有官方尺寸重采样，不能理解为逐像素读取保证。[Vision][vision] |
| 单图与请求 | inline／URL 单图 32 MiB；请求 JSON 48 MiB；`file_id` 单图 64 MiB | 必须检查编码后的请求大小；32 MiB 原图的 Base64 已接近请求总限。[Vision][vision] |
| 数量与总量 | 最多 600 图；无 `file_id` 总图像 64 MiB，有则最多 200 MiB；单边 8192 px，15 图以上降至 4096 px | 这些是服务上限，不是建议批量或本地预算默认值。[Vision][vision] |
| 外部 URL | 最长 8192 字符、下载需在 60 秒内完成 | URL 字符串相同不证明图片字节未变。[Vision][vision] |
| Files | `POST /files`，purpose=`user_data`；64 MiB、上传 10 分钟；属于 API Key，可指定过期，未指定则永久保留 | 若将来采用，应单列上传／复用／删除与凭据变更契约；本轮未上传。[Files API][files] |

**消息角色需分开读契约。** Vision guide 写 Chat 图片只在 user；同日 Chat API reference 的 tool message schema 又明确允许含图片的 content 数组。两处措辞有差异，本轮不能把 Chat 工具图片宣布为在线验收通过或断言不支持。Responses 文档则明确允许 user／developer 及 function/custom tool output 图片，system／assistant 图片返回 400。[Vision][vision]、[Chat API][chat]、[Responses][responses]

Responses 的 `input_file` 通用文件输入不受支持，不等于 `input_image.file_id` 不受支持。`file_id` 与 `image_url` 互斥，使用文件引用时忽略 `detail`。[Responses API][responses-api]

## 丢图路径与已执行证据

实际路径为 `ManagedAgentModel → Agents LitellmModel → Converter.items_to_messages → litellm.acompletion → DeepSeekChatConfig._transform_messages → HTTPX`；直接 `CompletionExecutor.acomplete` 从 LiteLLM 这一段进入，同样遇到压平。[ManagedAgentModel][managed]、[SDK 调用][sdk-litellm]、[执行器][executor]

1. Agents 对 user 走 `extract_all_content`；把 `input_image` 转为 `image_url`，将 `original` 改为 `high`。DeepSeek 目前定义这两个 detail 等效。[SDK 转换][sdk-converter]、[Vision][vision]
2. Agents 的 LiteLLM 接口设置 `preserve_tool_output_all_content=True`，function output 变成 role=tool 的内容数组；不是原生 `/responses` 请求。[SDK 调用][sdk-litellm]、[SDK 转换][sdk-converter]
3. DeepSeek 的 `_transform_messages` 在第 128 行无条件调用 `handle_messages_with_content_list_to_str_conversion`。后者抽取每块 `text` 并串接，只要得到非空文本就覆盖整个 `content`，图块丢失。[DeepSeek 变换][deepseek-transform]、[压平函数][flatten]
4. 只有图片而没有非空文字时，压平函数不覆盖列表，所以图片可能意外保留。该细节解释了为何“所有 DeepSeek 图片都丢失”不成立。[压平函数][flatten]；以下实测。

离线端点复用仓库 `running_model_service`，只发送公共的 1×1 PNG，固定 `fixture` Key；socket 禁止非 loopback 连接、关闭 tracing／telemetry。HTTP 返回统一假成功，**不验证服务输入合法性、图像识别或付费情况**。探针完整源码见文末；表中图片数从实际请求体而非原始调用参数计算。[端点夹具][fixture]、[图片回执][images]

| 探针路径／改动（仅临时进程） | 实际图片数 | 观察 |
| --- | --- | --- |
| ManagedAgent，DeepSeek，user 图文 | 0 | `content` 变为字符串；回执也为 0 |
| CompletionExecutor，DeepSeek，user 图文 | 0 | 绕过 Agent 无法绕过 provider 压平 |
| ManagedAgent，DeepSeek，工具输出图文 | 0 | tool content 变为字符串 |
| ManagedAgent，DeepSeek，工具输出仅图 | 1 | tool content 列表保留；不证明官方服务接受 |
| 仅加入 `supports_vision=True` 元数据，user 图文 | 0 | 能力标记已 True，实际请求仍无图 |
| 临时用基类 `_transform_messages` 替换 DeepSeek 压平，user／tool／direct 各一条 | 各 1 | 三条都保留图片；该探针不构成生产补丁 |
| `openai/deepseek-flash` 兼容路由，ManagedAgent／direct 各一条 | 各 1 | 请求 model 仍 `deepseek-flash`；端点为 localhost |
| 原生 OpenAI SDK `/responses`，user／tool 各一条 | 各 1 | 保留 `original`；此探针没有经过共享执行器 |
| Agent `input_image.file_id` | 无 HTTP | Converter 抛 `UserError`，仅支持 image URL |
| 普通 DeepSeek 文字对照 | 0（符合预期） | 1 次调用；假响应用量正常返回 |

另外三条实际参数探针显示：`thinking={type:disabled}` 在 DeepSeek 路由被移除；`reasoning_effort=max` 只留下 `thinking=enabled`，effort 本身消失；Chat `response_format=json_schema` 原样发出。对应源码只接受 enabled，且把任何非 none effort 映射为 enabled。[参数映射][deepseek-transform]

这与官方默认启用 thinking、支持 disabled 及具体 effort 的现行契约存在偏差。不能声称已按“关闭思考／固定 effort”冻结处理策略，直到实际请求参数符合预期。[Thinking][thinking]

现有定点测试通过 **10 passed，4 个依赖内部 Pydantic 弃用警告**：图片回执两路、Agent 重试含流式两路、工具不重放、缺 usage 保持未知、取消待清理、流中断不重放、并发预算、期限释放 slot。后六类主要用既有文本假响应；不是 DeepSeek 图片服务验收。[Agent 测试][agent-tests]、[执行器测试][executor-tests]

## 结构化输出、用量与缓存

Chat 文档列出的 `response_format.type` 只有 `text` 与 `json_object`；JSON mode 要求提示包含 JSON 指令并设输出预算，仍可能空内容或截断。当前客户端能发 `json_schema` 不证明服务保证 schema；不能直接把 Agent `output_type` 的 schema 请求当成契约成立。[Chat API][chat]、[JSON guide][json]、[SDK 调用][sdk-litellm]

Responses reference 列出 `text.format` 的 `json_schema`；其 schema 可约束输出形状，但不能保证图中关系真实、OCR 判断正确或出处对应。无论走哪路，本地仍验证字段、枚举、非空内容、区域来源和完成状态；截断／内容过滤／失败不能以局部 JSON 作为成功描述。[Responses API][responses-api]

建议候选输出只表达可见对象、可见关系、图内文字、无法确认之处及 OCR 建议；应用结合可靠文字层／扫描证据裁定 OCR，模型不能覆盖全扫描暂停规则。具体 schema、OCR 循环次数和“可用描述”门槛留在地图已有结果／路由决策中；这不是本研究替用户选择字段。

服务图片与文字共同计入输入 token；图片估算受尺寸重采样影响，官方当前给出每图上限 1024 token，最终以返回 usage 为准，不能由“发送了 1 张图”推导精确图像账单。[Vision][vision]、[Token Usage][tokens]

现有 ledger 归一化总输入、缓存输入、总输出和 reasoning token，不单列 image token，也不保存响应 model／system_fingerprint。回执缺失保持未知；取消或丢失最后流块时，不能记为零费用。若需保存服务模型指纹，应在识别产物元数据中明确，不假称现有 ledger 已提供。[用量源码][usage]、[Chat API][chat]

现有图片回执对 Base64 哈希实际解码字节，对 URL 只哈希 URL 字符串；不支持 `file_id` 图片身份。它证明客户端 HTTP 请求带有对应块，不能证明服务器读取正确、模型看见细节或描述可靠。[图片回执][images]

本地描述缓存建议固定：原图／实际裁剪字节、实际裁剪范围（相对输入图像，不含宿主页或出现位置坐标）及渲染策略、实际提供的图内转写、提示与 schema 版本、供应商端点与请求模型、detail、thinking／生成参数。原页定位和回映变换单独保存在各 occurrence；同图可跨出现位置复用描述。宿主图注和正文不得进入共享图内描述输入或缓存。这是地图既定边界的实现推论，不是供应商保证。

DeepSeek 上下文缓存是前缀命中机制；不是本地描述结果缓存。别名升级后重跑未必同结果，旧引用可读必须依靠保存的版本化产物，而不能依靠以后再次请求同一模型名重建。[上下文缓存][cache]、[模型别名][models]

## 最小候选与共享运行时边界

| 候选 | 已有证据 | 必须补齐及影响 |
| --- | --- | --- |
| A：审阅内置 DeepSeek 适配器的有限兼容修复 | 临时跳过压平后 user／tool／direct 均送图 | 同时审阅 thinking／effort 映射和能力表；对旧文字模型保留明确行为；不需要顺带升级全依赖 |
| B：固定端点的 OpenAI-compatible 路由 | `openai/deepseek-flash` 可送出图片 | 真实提供方仍是 DeepSeek；需证明参数、认证、费用归属、模型标识均正确。不能把更换前缀当成验收或全局迁移 |
| C：新增受管理的原生 Responses 传输 | 官方提供接口，锁定 OpenAI SDK 可序列化 user／tool 图片 | 当前执行器返回值／参数为 Chat 形状；要接入预算、取消、用量、失败状态，工程面更大 |

研究建议先评审 A；B 可作有边界的兼容备选；C 仅在已有决策确有原生 Responses 能力需求时选择。升级依赖也是后续可审阅路径，本轮没有调查出某个“升级即全部修好”的版本，不能擅自执行。

现有运行时必须复用，但尚缺以下接入能力：[执行器][executor]、[ManagedAgentModel][managed]、[配置快照][snapshot]

- `RoleBindings` 当前没有 vision 角色，所有角色共用一个 credential bundle；`ManagedAgentModel` 固定 answer。独立图片理解连接需要明确角色／凭据绑定，不能偷偷复用主 LLM Key。
- 锁后一次捕获配置的规则继续有效；图内描述应消费该快照。当前 `public_identity` 不含端点，因此识别缓存不能直接拿 executor fingerprint 代替完整处理身份。
- 调用次数、并发、有限重试、deadline、取消由任务执行器管理；新建第二个 executor 会新建 `_calls`，共享同一个 `ModelCallPolicy` 对象也不会共享计数器。必须明确共同预算所有者或有界子预算。
- 单次图内描述可用直接 CompletionExecutor；若确需 Agent 工具循环，继续由 SDK 管循环／重试，不能外面再叠一轮自动重试。语义校验后的修复请求也必须消耗预算。
- 异步取消会取消并等待本地请求任务清理，晚到结果禁止发布；没有证据说明关闭客户端连接必然撤销服务端计算或收费。Responses 是无状态接口，`background`／服务会话续接不是恢复机制。[Responses][responses]
- 普通 description 失败按既定识别缺口处理，不能升级为全扫描正文 OCR 的失败。其图像出处与描述结果必须独立于主知识编译成功保存。

## 后续最低验证矩阵（均尚未执行真实服务验收）

| 验证项 | 最小方法与通过证据 |
| --- | --- |
| 传输与角色 | 已批准端点＋小型合成图；对照文字请求；记录发送图像摘要、返回 request id；user 为首版必测，tool／file 路径仅采用时测 |
| 图内边界 | 相同图片置于两份有矛盾正文的合成 PDF；请求只能出现同一图和图内转写，描述缓存身份一致，各 occurrence 出处不同 |
| 裁剪可读性 | 小字、细箭头、旋转、矢量示意图；检查实际渲染裁剪及图中局部放大，人工标注可见对象／关系，不以自报置信度代替质量证据 |
| 转写与 OCR 建议 | 无字、可读文字、模糊正文、图内 OCR 与图冲突各一例；应用路由保持已确认规则，描述不能替代失败正文 OCR |
| 参数与输出 | 请求回执核对 detail、thinking、effort；空字符串、坏字段、截断、过滤、HTTP 错误均走有界失败，不生成伪成功描述 |
| 取消与预算 | 发送前、在途、重试等待、最后回复后取消；验证无额外发送、清理完成、晚到不发布；已发生用量保留、未知不当零 |
| 用量 | 正常／流式／中断；总量对照服务账单或 usage，图像估算单独标“估算”；模型表价格不是实时报价 |
| 版本与复用 | 相同图＋转写命中缓存；转写／裁剪／端点／模型策略变化生成新处理身份；仅换 Key 不强制重描，旧出处仍能打开 |

方案 A／B／C、视觉绑定如何共用预算、输出契约与失败归类可纳入地图已有决策；目前没有必须另开独立决策票的事实。完成本研究只解除事实未知，不代表可跳过上述验收。

只有 A 无法满足既定能力时，才形成新增取舍：“首版继续修复内置 Chat 适配，还是接受原生 Responses 及新增受管理传输的实施范围？”前者重点为有限兼容回归，后者扩大参数／取消／用量接口；依赖升级须另有版本审阅证据。

## 可复现离线探针

下面源码即执行版本，SHA-256 为 `4b816b4f0688b279110a61f448e4d0917401f31463e8f8405ab9b322f489fa74`。保存为 `/tmp/deepseek-transport-probe-20261008.py`，在基线工作树运行；复用已安装并核对版本的 Python，不运行安装命令。完整结果只写 `/tmp/deepseek-transport-probe-results-20261008.json`。

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH="$PWD:$PWD/vendor/LiteLLM:$PWD/tests" /home/cloudyi/CodeWorkspace/UrltraKB/.venv/bin/python /tmp/deepseek-transport-probe-20261008.py
```

现有测试执行时也使用同样 PYTHONPATH、loopback socket 限制和 `-p no:cacheprovider`；选择上文八个 test function（两个有参数化，总计十例）。测试与探针都没有生产代码改动。

```text
tests/test_managed_agent.py::test_image_receipt_observes_actual_provider_payload
tests/test_managed_agent.py::test_agent_retry_first_send_is_single_and_accounted
tests/test_managed_agent.py::test_agent_tool_is_not_replayed_after_next_request_fails
tests/test_managed_agent.py::test_stream_without_usage_remains_unknown
tests/test_managed_agent.py::test_agent_cancel_waits_for_cleanup_without_retry
tests/test_managed_agent.py::test_failed_stream_after_text_is_never_replayed
tests/test_llm_execution.py::test_parallel_calls_cannot_overspend_budget
tests/test_llm_execution.py::test_deadline_stops_retry_and_releases_concurrency_slot
```

```python
"""Offline research probe. All sockets are restricted to loopback; no real API call."""
import asyncio
import copy
import hashlib
import importlib.metadata
import json
import os
import socket
from pathlib import Path
from unittest.mock import patch

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["OTEL_SDK_DISABLED"] = "true"

_connect = socket.socket.connect
def local_connect(self, address):
    if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
        raise RuntimeError("Research probe blocks external sockets")
    return _connect(self, address)
socket.socket.connect = local_connect

import litellm
import pytest
from agents import Agent, ModelSettings, RunConfig, Runner, set_tracing_disabled
from agents.models.chatcmpl_converter import Converter
from openai import AsyncOpenAI
from openkb.agent.managed_model import ManagedAgentModel
from openkb.config import LlmCredentialBundle
from openkb.llm_execution import CompletionExecutor, ModelCallPolicy, ModelRequest, RoleBindings
from openkb.llm_images import image_digest, request_image_digests
from litellm.llms.deepseek.chat.transformation import DeepSeekChatConfig
from litellm.llms.openai.chat.gpt_transformation import OpenAIGPTConfig
from test_vendor_sdk import running_model_service, response

set_tracing_disabled(True)
litellm.telemetry = False
litellm.suppress_debug_info = True
IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jBv8AAAAASUVORK5CYII="
PARTS = [{"type":"input_text","text":"Only describe visible image contents; return JSON."},
         {"type":"input_image","image_url":IMAGE,"detail":"original"}]
USER = [{"role":"user","content":PARTS}]
TOOL = [{"role":"user","content":"Read the screenshot returned by the tool."},
        {"type":"function_call","call_id":"call-fixture","name":"fixture_image","arguments":"{}"},
        {"type":"function_call_output","call_id":"call-fixture","output":PARTS}]
TOOL_ONLY = copy.deepcopy(TOOL)
TOOL_ONLY[-1]["output"] = [PARTS[1]]

def make_executor(service, model, **policy):
    return CompletionExecutor(RoleBindings(model,LlmCredentialBundle(api_key="fixture",base_url=service.url)),
                              ModelCallPolicy(retries=0,**policy))

def summary(body, path):
    content = body.get("messages",body.get("input",[]))
    def details(value):
        found=[]
        if isinstance(value,dict):
            if "detail" in value: found.append(value["detail"])
            for item in value.values(): found += details(item)
        elif isinstance(value,list):
            for item in value: found += details(item)
        return found
    return {"path":path,"model":body.get("model"),"images":len(request_image_digests(content)),
            "roles":[v.get("role",v.get("type")) for v in content],
            "content_shapes":[type(v.get("content",v.get("output"))).__name__ for v in content],
            "detail":details(content),"thinking":body.get("thinking"),
            "reasoning_effort":body.get("reasoning_effort"),"response_format":body.get("response_format")}

async def managed(service, model, items):
    service.replies.append((200,response()))
    executor=make_executor(service,model)
    model_obj=ManagedAgentModel(executor)
    result=await Runner.run(Agent(name="probe",model=model_obj,
                                 model_settings=ModelSettings(retry=model_obj.retry_settings(),include_usage=True)),
                            copy.deepcopy(items),run_config=RunConfig(tracing_disabled=True))
    body, _, path=service.records[-1]
    return summary(body,path)|{"receipt_images":len(result.raw_responses[0].openkb_image_digests),"calls":executor.calls}

async def direct(service,model,items,options=None):
    service.replies.append((200,response()))
    executor=make_executor(service,model)
    messages=Converter.items_to_messages(copy.deepcopy(items),preserve_tool_output_all_content=True,model=model)
    result=await executor.acomplete("compile",ModelRequest(messages,operation="research",stage="vision-probe",generation_options=options))
    body, _, path=service.records[-1]
    return summary(body,path)|{"calls":executor.calls,"raw_usage_available":result.raw_usage_available}

async def native_responses(service,items):
    service.replies.append((200,{"id":"resp-fixture","object":"response","created_at":1,"status":"completed",
        "model":"deepseek-flash","output":[],"parallel_tool_calls":True,"tool_choice":"auto","tools":[],
        "usage":{"input_tokens":12,"output_tokens":3,"total_tokens":15}}))
    async with AsyncOpenAI(api_key="fixture",base_url=service.url,max_retries=0) as client:
        await client.responses.create(model="deepseek-flash",input=copy.deepcopy(items))
    body, _, path=service.records[-1]
    return summary(body,path)

async def main():
    rows={}
    with pytest.MonkeyPatch.context() as monkeypatch, running_model_service(monkeypatch) as service:
        rows["managed_deepseek_user"]=await managed(service,"deepseek/deepseek-flash",USER)
        rows["completion_deepseek_user"]=await direct(service,"deepseek/deepseek-flash",USER)
        rows["managed_deepseek_tool_text_image"]=await managed(service,"deepseek/deepseek-flash",TOOL)
        rows["managed_deepseek_tool_image_only"]=await managed(service,"deepseek/deepseek-flash",TOOL_ONLY)
        rows["managed_openai_route_user"]=await managed(service,"openai/deepseek-flash",USER)
        rows["completion_openai_route_user"]=await direct(service,"openai/deepseek-flash",USER)
        with patch.dict(litellm.model_cost,{
            "deepseek/deepseek-flash":{"litellm_provider":"deepseek","mode":"chat","supports_vision":True},
            "deepseek-flash":{"litellm_provider":"deepseek","mode":"chat","supports_vision":True}}):
            rows["metadata_only_user"]=await managed(service,"deepseek/deepseek-flash",USER)
            rows["metadata_only_user"]["supports_vision"]=litellm.supports_vision("deepseek/deepseek-flash")
        with patch.object(DeepSeekChatConfig,"_transform_messages",OpenAIGPTConfig._transform_messages):
            rows["memory_transform_override_user"]=await managed(service,"deepseek/deepseek-flash",USER)
            rows["memory_transform_override_tool"]=await managed(service,"deepseek/deepseek-flash",TOOL)
            rows["memory_transform_override_direct"]=await direct(service,"deepseek/deepseek-flash",USER)
        rows["thinking_disabled"]=await direct(service,"deepseek/deepseek-flash",USER,{"thinking":{"type":"disabled"}})
        rows["reasoning_max"]=await direct(service,"deepseek/deepseek-flash",USER,{"reasoning_effort":"max"})
        rows["chat_json_schema"]=await direct(service,"deepseek/deepseek-flash",USER,{"response_format":{"type":"json_schema","json_schema":{"name":"probe","schema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":False},"strict":True}}})
        rows["native_responses_user"]=await native_responses(service,USER)
        rows["native_responses_tool"]=await native_responses(service,TOOL)
        rows["text_control"]=await direct(service,"deepseek/deepseek-flash",[{"role":"user","content":"hello"}])
    try:
        Converter.items_to_messages([{"role":"user","content":[{"type":"input_image","file_id":"file-api-fixture"}]}],model="deepseek/deepseek-flash")
    except Exception as error:
        rows["agent_file_id"]={"error":type(error).__name__,"message":str(error)}
    no_image={"managed_deepseek_user","completion_deepseek_user","managed_deepseek_tool_text_image",
              "metadata_only_user","thinking_disabled","reasoning_max","chat_json_schema","text_control"}
    for name,value in rows.items():
        if name != "agent_file_id":
            assert value["images"] == (0 if name in no_image else 1), (name,value)
            if "receipt_images" in value: assert value["receipt_images"] == value["images"]
    assert rows["agent_file_id"]["error"] == "UserError"
    assert rows["metadata_only_user"]["supports_vision"] is True
    assert rows["thinking_disabled"]["thinking"] is None
    assert rows["reasoning_max"]["thinking"] == {"type":"enabled"}
    assert rows["reasoning_max"]["reasoning_effort"] is None
    assert rows["chat_json_schema"]["response_format"]["type"] == "json_schema"
    report={"versions":{p:importlib.metadata.version(p) for p in ["openai-agents","openai","litellm","httpx"]},
            "baseline":"be5aad49cffac16d36e4c676f099467a5ab3cc22","fixture_image_digest":image_digest(IMAGE),
            "probe_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"cases":rows}
    Path("/tmp/deepseek-transport-probe-results-20261008.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))

asyncio.run(main())
```

## 第一方来源索引

正文链接中的服务协议核对到调研日；仓库与依赖源码均固定到上述提交，避免后续分支变化改变证据。

[models]: https://api-docs.deepseek.com/quick_start/pricing/
[vision]: https://api-docs.deepseek.com/guides/vision/
[chat]: https://api-docs.deepseek.com/api/create-chat-completion/
[responses]: https://api-docs.deepseek.com/guides/responses_api/
[responses-api]: https://api-docs.deepseek.com/api/create-response/
[files]: https://api-docs.deepseek.com/guides/files_api/
[thinking]: https://api-docs.deepseek.com/guides/thinking_mode/
[json]: https://api-docs.deepseek.com/guides/json_mode/
[tokens]: https://api-docs.deepseek.com/quick_start/token_usage/
[cache]: https://api-docs.deepseek.com/guides/kv_cache/
[deps]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/pyproject.toml#L34-L62
[deepseek-transform]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/vendor/LiteLLM/litellm/llms/deepseek/chat/transformation.py#L27-L146
[flatten]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/vendor/LiteLLM/litellm/litellm_core_utils/prompt_templates/common_utils.py#L87-L155
[table]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/vendor/LiteLLM/litellm/model_prices_and_context_window_backup.json#L13043-L13117
[table-id]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/vendor/LiteLLM/litellm/_urltrakb_model_table.json
[sdk-litellm]: https://github.com/openai/openai-agents-python/blob/17f7caeaa33d97eee7152f9834af5e706b8f90e2/src/agents/extensions/models/litellm_model.py#L414-L558
[sdk-converter]: https://github.com/openai/openai-agents-python/blob/17f7caeaa33d97eee7152f9834af5e706b8f90e2/src/agents/models/chatcmpl_converter.py#L337-L775
[managed]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/openkb/agent/managed_model.py#L89-L197
[executor]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/openkb/llm_execution.py#L28-L582
[snapshot]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/openkb/config_state.py#L94-L157
[usage]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/openkb/llm_usage.py#L26-L213
[images]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/openkb/llm_images.py#L8-L50
[fixture]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/tests/test_vendor_sdk.py#L13-L80
[agent-tests]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/tests/test_managed_agent.py#L26-L204
[executor-tests]: https://github.com/wooveep/UrltraKB/blob/be5aad49cffac16d36e4c676f099467a5ab3cc22/tests/test_llm_execution.py#L49-L202
