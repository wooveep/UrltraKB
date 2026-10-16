# 默认容量与执行限制

UltraKB 的调用预算、模型输入容量、正文阅读配额、资料长短判定和任务调度限制分别生效。
`model_policy.max_calls=1000` 是一个逻辑模型任务的发送次数上限，不能当作导入总步骤或完成百分比。
以下数值对应 2026 年 10 月 16 日的代码，覆盖生产代码中影响模型、导入、检索、文件接入、
调度与产物的配置默认值和固定边界，并区分项目入口与所带依赖的独立默认值。
界面像素、日志预览长度、版本编号及测试额度不属于运行容量。
字符数指代码中的字符串长度；MiB 为 1024² 字节。

## 配置层与适用范围

**配置默认值**可在标明的配置层覆盖；**固定边界**没有同名用户配置项；
**动态容量**从捕获的模型设置和分词器计算；**未设置**表示 UltraKB 没有统一上限，
服务端、SDK、操作系统或其他配额仍可能限制它。

执行设置在取得知识库执行许可后捕获，同批后续单元沿用快照。配置文件修改不会改变
已经运行的任务。续跑恢复已消耗的模型预算，不会因重试自动获得全新额度。
依据：[执行上下文](../ultrakb/application/execution.py#L166)、
[配置快照](../ultrakb/config_state.py#L20)、[模型任务账本](../ultrakb/model_task.py#L46)。

| 配置项 | 生效位置 |
| --- | --- |
| `model_capacity`、`extraction_budget`、`office_timeout_seconds`、`pdf_short_max_pages` | 默认值 → 全局配置 → 知识库配置 |
| `model_policy`、`role_generation`、`index_navigation`、顶层 `concurrency`、`timeout` | 知识库 `.ultrakb/config.yaml`；不在当前全局继承白名单中 |
| `litellm.timeout` | 对同一知识库的顶层 `timeout` 优先；其他 SDK 配置按透传规则处理 |
| `ULTRAKB_*` 环境变量 | 只作用于明确读取该变量的入口，不等于知识库配置键 |

依据：[继承白名单与合并](../ultrakb/config.py#L488)、[请求参数优先级](../ultrakb/config.py#L329)。

## 模型调用次数与时间

`model_policy.*` 来自 [ModelCallPolicy](../ultrakb/llm_execution.py#L183)，
验证见 [配置验证](../ultrakb/config_validation.py#L67)。

| 参数或限制 | 默认值 | 作用范围与边界 |
| --- | --- | --- |
| `model_policy.max_calls` | 1000 次实际发送 | 一个逻辑模型任务，跨角色、索引、编译、修复和实际发送的重试共享；非负整数，0 表示不能发送，非无限制 |
| `model_policy.concurrency` | 5 | 单执行器模型并发，至少 1；不是整个桌面或账号的总并发 |
| `model_policy.retries` | 2 | 可重试传输失败的额外重试次数；首次发送另计，实际发送均消耗总额度 |
| `model_policy.backoff` | 0.25 秒 | 重试起始退避，有限非负数；Agents 适配器的退避上限为 10 秒 |
| `model_policy.deadline_seconds` | 未设置 | 可选任务时限，配置后须有限且大于 0；与单次 HTTP 超时不同 |
| `model_policy.compile_reserve_calls` | 3 次 | 索引不能花掉的编译预留，至少 2；不是额外增加的额度 |
| `model_policy.compile_repair_calls` | 1 次 | 编译修复准入，另受具体步骤自身的次数和总预算约束 |
| 检索阶段发送额度 | 每问题共享 8 次 | 知识与会话检索共用，含修复与重试；新问题重置阶段额度，不重置任务总数 |
| 会话索引阶段发送额度 | 6 次 | `conversation_index` 阶段，另受任务总预算约束 |
| 并发检索入口 | 1 | 每执行器固定限制 |
| 最终回答预留 | 1 次发送 | 普通工具不能花掉的最后回答容量，仍在任务总额度内 |
| 单次模型请求 `timeout` | UltraKB 未显式设置 | `litellm.timeout` 或顶层 `timeout` 可配置有限正数 |
| 普通 LiteLLM completion 缺省 HTTP 超时 | 600 秒 | 所带 LiteLLM 未收到显式超时且全局值为默认哨兵时采用；哨兵本身为 6000 秒，不能误记为 completion 缺省值 |
| 取消后补充收集已发送响应的计量等待 | 250ms | 计量收尾期限，不代表云端请求已取消或业务进程已回收 |

补充依据：[发送准入与阶段配额](../ultrakb/execution_budget.py#L11)、
[最终回答](../ultrakb/agent/answer_finalization.py)、[Agents 退避](../ultrakb/agent/managed_model.py#L141)、
[LiteLLM 超时](../vendor/LiteLLM/litellm/litellm_core_utils/completion_timeout.py)、
[计量收尾](../ultrakb/llm_usage_transport.py#L119)。

目前没有项目层统一默认的累计 token、金额、每分钟请求数或每分钟 token 硬上限。
账本记录消耗，`max_calls` 只限制次数，不能换算为固定费用或 token 总量。
提供方与显式配置的 SDK 规则可以另外限制这些维度。

编译校验失败的响应正文原样保存在 `.ultrakb/compile-responses/`，当前不设单响应或
总存储大小上限，也没有自动过期清理；成功发布与编译检查点清理会保留这些诊断。
依据：[失败响应存档](../ultrakb/compile_checkpoints.py)。

### 1000 次是否合理

1000 可作为防止无限调用的保护阈值，但当前实现没有依据文档页数、节点数、摘要批数或
整批资料数量自动计算它，不能把它视为经过各种规模验证的完成容量。

例如，一份资料索引使用 270 次、首个编译请求使用 1 次时，已消耗 271 次，资料仍可能
没有最终结果回执。若同一逻辑任务内四份资料各需 270 次索引发送，仅索引就需要 1080 次；
这是说明共享额度影响的条件计算，不是对其他资料的用量预测。
小问题则先受到检索阶段与代理轮数的更小约束。

保留 1000 作为当前保护默认值更稳妥。容量规划应按任务类型、文档结构和批量大小分别统计，
结合阶段准入与检查点恢复，再决定是否引入规模预估或可审查的额度调整。
调大次数不会扩展单次上下文、检索正文配额或提供方速率限制。

## 模型上下文与输出

| 参数或限制 | 默认值 | 作用范围与覆盖方式 |
| --- | --- | --- |
| `model_capacity.max_input_tokens` | 未设置 | 显式输入上限，正整数；按捕获的本地输入计量器计算 |
| `model_capacity.max_input_bytes` | 未设置 | 显式 UTF-8 字节护栏；与 token 模式互斥，不声称是模型 token 容量 |
| `model_capacity.max_request_bytes` | 未设置 | 独立的应用请求字节护栏，可与 token 容量同时设置 |
| `model_capacity.context_window_tokens` | 未设置 | 完整上下文窗口，不是单份资料的长度上限 |
| `model_capacity.output_reserve_tokens` | 未设置 | 输出空间预留；不能将缺省当作统一输出额度 |
| `model_capacity.max_output_tokens` | 未设置 | 服务单次输出最大值；请求输出额度不得超过它 |
| `model_capacity.measurement` | `tokenizer` | 可显式选择 `utf8_token_upper_bound`；这是保守 token 上界估算，不是应用字节限制 |
| `model_capacity.tokenizer_model` | 未设置 | 标准已知模型可用所带目录及受支持分词器；私有端点不猜测别名或分词器 |
| `model_capacity.applies_to_model` | 未设置 | 容量预设可绑定模型；不匹配时拒绝误用 |
| 输入与预留关系 | `输入上限 ≤ 窗口 − 输出预留` | 预留须小于窗口；只给窗口而不给预留时，不能确认有效输入容量 |
| 内置连接示例 | 按精确模型和官方端点选择能力档案 | DeepSeek Flash：1,000,000 token 上下文、393,216 最大输出、65,536 默认输出预留；OpenAI 示例使用锁定 SDK 的能力目录。来源随档案保存 |
| 其他模型与私有端点容量 | 未确认时明确报错或报告未知 | 没有所有模型默认 64K 的规则；取决于角色与捕获配置 |
| 普通回答、索引、编译 `max_tokens` | 已知上下文档案采用其输出预留 | 可用对应 `role_generation` 显式设置；输入预算按本次实际输出额度重新计算 |
| `model_policy.retrieval_tokens` | 1024 输出 tokens | 检索结构输出，可选 1024、2048、4096；不是原文阅读配额 |
| `model_policy.retrieval_repair_tokens` | 8192 输出 tokens | 检索格式修复，范围为初始输出额度至 8192 |
| `model_policy.retrieval_repairs` | 2 次 | 每问题允许 0–2 次，另消耗共享 8 次检索额度 |
| `role_generation.*retrieval.max_tokens` | 跟随检索默认值 | 显式指定时只能取 1024、2048、4096 |

首次捕获新任务配置时，在设置锁和知识库写锁下识别并迁移旧连接示例的精确
`max_input_tokens: 65536 + local:utf8-bound` 组合；保存迁移前后收据，再原子更新配置。
迁移只处理可证明来自旧工厂的预设，不删除 tokenizer、历史快照、成功摘要或原文。
私有端点没有官方能力依据时清除旧示例并要求显式声明容量。用户自行设置的输入或字节限制保留。
应用字节模式可显式设置 `max_input_bytes`，不需要下载 tokenizer；新配置也可把独立的
`max_request_bytes` 叠加在 token 容量上。能力未知时停止模型请求，不猜一个统一边界。
输入预算取显式 token 输入上限与“上下文减本次输出额度”的较小值。
完整 SDK 输入由同一入口组装、计量和发送，包括生成选项、响应 schema、工具与修复信息。
设置采用嵌套合并补丁；切换模式时须显式清除另一模式的字段，GUI 的完整 JSON 编辑会包含这些清除项。

概念/实体新建页按完整请求分批，计量包含原文、定位、摘录和图片目录、响应格式及捕获的生成参数。
每批也提前检查有限的修复提示。原文覆盖通过来源版本、连续偏移和逐字符内容核验，不能靠截断满足容量。
成功批次分别保存检查点；恢复只调用缺失批次，实际发送继续扣减原逻辑任务的共享次数预算。
长文原始页和图片先于模型导航保存；索引的已验证目录、各节点摘要和已收到响应独立于发布回滚保存。
导航描述按完整请求预算分组、逐层归约；可选描述失败标记为 pending，不撤销完整原文和核心索引。
知识摘要按原始范围分批；输出截断、缺失章节或原文引用校验失败，在一次有界修复后仍失败时，
逐级缩小原始范围并持久化拆分关系。恢复重用已接受子批次；最终正文在本地按原始顺序组合，
不再将所有知识正文塞入一个最终模型调用。不能继续缩小的失败保持显式失败，不截断或绕过原文校验。
长文摘要收到响应后，先原子保存正文、请求身份和校验和，再进行原文校验；重启可在本地验证
这份接收态结果，无须重新发送或把未经校验的内容当作成功。旧任务若已记录 HTTP 成功、
却在接收态落盘功能引入前丢失内存正文，只能通过显式“重试失败摘要”取得一次有收据的恢复机会，
仍扣减原任务的累计调用预算。结构规则同时进入初始请求组装与容量计量，避免只在失败后提示模型。
容量配置保存于执行快照；新规范化原文的内容指纹不再包含容量，旧指纹及已有检查点保持原样。
拼接时由程序依据实际读取位置汇总本页来源范围，并给各节标明局部来源；不以计划中的完整范围
代替实际读取范围，不把局部的“未读取”说明当作整页缺失。所选正文范围不表示整份资料已完整识别，
原文限制和识别缺口继续保留。此展示步骤保留已有正文、提示词和检查点签名，不增加模型调用。

依据：[容量模型](../ultrakb/processing_policy.py#L44)、[计算](../ultrakb/execution_capacity.py#L21)、
[连接预设](../ultrakb/application/connection_capacity.py#L9)、[分词计量](../ultrakb/model_tokenizer.py#L43)、
[检索输出](../ultrakb/context/condb_llm.py#L71)。完整请求容量包括指令、消息封装、工具模式与修复输入，
不只统计原文字数；本地计量不替代服务端用量回执。

## 问答与会话上下文

以下为固定实现策略，除明确提供函数参数的项外，没有同名知识库配置键。

| 限制 | 当前值 | 作用范围 |
| --- | --- | --- |
| 问答与对话代理轮数 | 50 | 单次 Agents 回答的最大轮数，不是 token 数 |
| 语义知识检查代理轮数 | 50 | 单次语义检查 |
| 单问题知识正文总配额 | 6000 tokens | 跨检索工具调用累计，重复检索不重置 |
| 知识检索束宽 / 最大轮数 / 最多候选 | 3 / 8 / 6 | UltraKB 显式传给 ConDB，覆盖依赖包示例设置 |
| 最近用户意图 | 最多 6 轮 | 容量不足时减少旧轮次 |
| 启动会话历史索引的阈值 | 8000 tokens | 已提交历史用户内容累计量，不是会话存储上限 |
| 会话意图召回正文 | 最多 2000 tokens | 还受当前请求剩余容量限制 |
| 会话召回束宽 / 最大轮数 / 最多历史轮次 | 3 / 8 / 6 | `select_k` 函数参数允许 1–6 |
| 会话准备的固定空间预留 | 6000 + 512 tokens | 为资料上下文与封装预留后，再分配历史意图空间 |
| ChatIndex 每节点子项数 | 5 | 构造参数至少 2；不是最多保存 5 轮对话 |
| ConDB 结构输出解析尝试 | 最多 2 次 | 初始输出加一次修复；仍受检索修复和总额度约束 |
| 原文证据搜索 | 1–6 个词，每词 ≤80 字符，最多 12 个命中 | 编译证据定位工具的固定边界 |
| 导航投影预览 | 索引摘要 800 字符；块 400；页与其他叶子 500 | 只缩短定位文本，不截断保留原文 |
| 导航投影组大小 | 12 个单元格 / 4000 字符 | 导航单元，不是生成知识的输出长度 |

依据：[回答轮数](../ultrakb/agent/query.py#L25)、[语义检查](../ultrakb/agent/linter.py#L17)、
[知识检索](../ultrakb/context/knowledge.py#L52)、[会话准备](../ultrakb/context/preparation.py#L12)、
[召回](../ultrakb/context/recall.py#L67)、[会话树](../ultrakb/conversation_index.py#L27)、
[解析](../ultrakb/context/condb_llm.py#L104)、[证据搜索](../ultrakb/agent/evidence_search.py#L19)、
[导航投影](../ultrakb/context/projection.py#L76)。

## 资料长短判定与索引

| 参数或限制 | 默认值 | 作用范围与边界 |
| --- | --- | --- |
| `pdf_short_max_pages` | 10 页 | PDF 及分页资料不超过 10 个物理页为短文；允许非负整数 |
| 旧 `pageindex_threshold` | 兼容投影为 11 | 显式旧值代表从该页数开始索引，≤0 强制索引；10 和 11 不是两个独立阈值 |
| 连续文本与工作表短文阈值 | 5000 tokens，含边界 | 固定离线 `cl100k_base` 业务长度，不随执行模型变化 |
| 连续文本内容块目标 | 2000 字符 | 优先自然结构与字素边界；单个超大字素可超过目标，非严格上下文上限 |
| `index_navigation.max_navigation_depth` | 8 | 可配置 1–32 |
| `index_navigation.max_nodes_per_unit` | 4 | 可配置 1–32 |
| `index_navigation.max_subtree_attempts` | 2 | 可配置 1–3 |
| PageIndex `toc_check_page_num` | 20 | 目录检查范围，不是整份资料页数上限 |
| PageIndex `max_page_num_each_node` | 10 | 节点拆分策略的页数阈值 |
| PageIndex `max_token_num_each_node` | 20000 | 节点拆分策略的 token 阈值 |
| PageIndex 模型并发 | 5 | 依赖缺省；知识库顶层 `concurrency` 可配置索引限制，另受执行器并发约束 |
| PageIndex 全文索引外层尝试 | 最多 3 次 | 仅对允许重试的目录准确性失败；确定性结构错误与终止异常不盲目整份重试 |
| PageIndex 独立调用超时 | 120 秒 | `PAGEINDEX_LLM_TIMEOUT` 可覆盖；UltraKB 注入共享执行器时使用捕获的统一请求设置，不能套为 GUI 的统一超时 |

依据：[分页](../ultrakb/processing_policy.py#L66)、[文本](../ultrakb/processing_policy.py#L100)、
[内容块](../ultrakb/content_blocks.py#L74)、[索引缺省](../vendor/PageIndex/pageindex/config.py#L12)、
[项目入口](../ultrakb/indexer.py#L145)、[共享模型适配](../ultrakb/index_llm.py#L8)。
短文也可能因完整请求超过输入容量而分段执行；长短分类与执行方式是两个维度。

## 知识编译与原文窗口

| 限制 | 默认值或固定值 | 说明 |
| --- | --- | --- |
| 编译并发 | 5 | 顶层 `concurrency` 或调用方参数可覆盖，另受模型执行器约束 |
| 规划概览 | 12000 字符 | 只限制给概念与实体规划的概览，不截断保留原文与完整摘要 |
| 摘要自然分段目标 | 4000 字符 | 在自然块结尾形成候选段；最终批次仍按完整请求容量装配 |
| 摘要发送准入 | 批次数 + 1 次 | 发送前检查摘要与后续计划能否容纳，不由默认 1000 推算批数 |
| 每页原始证据单元数 | 1–6 | 长文概念或实体页面的证据选择范围 |
| 默认原文读取窗口 | 24000 字符 | `read_selection` / `iter_selection` 可传预算，另受完整输入容量约束 |
| 通用证据载荷回退 | 48000 字符 | 未提供具体计量函数时的序列化载荷预算 |
| 窗口前后补充上下文 | 初始各 1200 字符 | 容量紧张时缩小至 32 字符边界后仍按可容纳性处理 |
| 更新生成的起始窗口 | 24000 字符 | 容量不足时减半；缺少容量计量器时也采用 24000 字符回退检查 |
| 编译计划与页面语义修复 | 每步骤最多一次修复 | 初始生成加一次修复，仍受总预算限制 |
| 代码示例解析 | 64000 字符、4096 AST 节点、深度 32 | 验证边界，不是整页输出长度上限 |
| 数值表达式校验 | 120 字符、64 AST 节点、整数幂指数绝对值 ≤12 | 固定可分析表达式边界 |
| 原始上下文检查摘要 | 最多 8 个区域、4 个相邻动作 | 校验和定位摘要，不是原文导入数量 |

依据：[编译](../ultrakb/agent/compiler.py#L1675)、[摘要](../ultrakb/agent/document_summary.py#L81)、
[准入](../ultrakb/execution_budget.py#L137)、[窗口](../ultrakb/agent/source_windows.py#L561)、
[补充上下文](../ultrakb/agent/source_window_ranges.py#L124)、[载荷](../ultrakb/agent/evidence_payload.py#L344)、
[更新](../ultrakb/agent/update_generation.py#L78)、[计划修复](../ultrakb/agent/compile_plan.py#L211)、
[页面修复](../ultrakb/agent/compiler.py#L542)、[示例](../ultrakb/agent/code_examples.py#L18)、
[数值](../ultrakb/agent/page_updates.py#L123)、[原始上下文](../ultrakb/original_context.py#L171)。

## 文件接入与转换

| 参数或限制 | 默认值 | 作用范围与覆盖方式 |
| --- | --- | --- |
| REST 单文件上传 | 100 MiB | `ULTRAKB_MAX_UPLOAD_FILE_BYTES`；不是桌面本地导入上限 |
| REST 请求上传总量 | 500 MiB | `ULTRAKB_MAX_UPLOAD_REQUEST_BYTES` |
| REST 上传读取块 | 1 MiB | 流式缓冲，不是文件容量 |
| URL 请求超时 | 30 秒 | 网页或远程文件获取的固定设置 |
| URL 下载读取块 | 64 KiB | 缓冲大小；该入口没有统一下载总字节上限 |
| HTML 提取质量提示阈值 | 300 字符 | 较短仍可保留并报告提示，不等于导入拒绝阈值 |
| URL 文件名主干 | 80 字符 | 文件名规整限制 |
| 下载远程图片 | 默认关闭 | `download_remote_assets=false`；开启后每图 ≤20 MiB，请求超时 15 秒 |
| PDF 图片提取最小尺寸 | 32 像素 | 微小资产过滤，不是原件精度上限 |
| `office_timeout_seconds` | 120 秒 | Office 转换，整数 1–3600，可全局或按库配置 |
| Office 可用性探测 | 20 秒以内 | 不超过当前 Office 转换时限 |
| CNKI 转换超时 | 120 秒 | `convert_cnki(timeout=...)` 函数参数，非同名知识库配置项 |
| CNKI 页与对象验证 | 页数 ≤100000、对象号 ≤1000000 | 固定格式完整性检查 |
| 原生表格占位数 | 16384 | PDF 与源表格结构保护 |
| 源 HTML 表格输入 | 1000000 字符 | 表格解析保护 |
| 工作表可选坐标 | 1048576 行 × 16384 列 | 有限区域的格式边界，不按整张网格扩展稀疏数据 |

依据：[上传](../ultrakb/api_helpers.py#L53)、[URL](../ultrakb/url_ingest.py#L32)、
[远程图片](../ultrakb/remote_assets.py#L15)、[图片](../ultrakb/images.py#L32)、
[Office](../ultrakb/config_validation.py#L36)、[探测](../ultrakb/office/runtime.py#L82)、
[CNKI](../ultrakb/cnki/convert.py#L18)、[CNKI 验证](../ultrakb/cnki/engine.py#L42)、
[表格](../ultrakb/source_tables.py#L8)、[工作表](../ultrakb/workbooks/selection.py#L19)。

## 嵌入文档提取与派生任务

以下来自 [ExecutionBudget](../ultrakb/pending/records.py#L11)，可用 `extraction_budget` 全局或按库配置。
这些额度控制一组嵌入对象的发现与提取，不是普通本地原始文件的统一大小上限。

| 参数或限制 | 默认值 | 范围 |
| --- | --- | --- |
| `max_depth` | 8 | 嵌套提取深度，允许 0–100 |
| `max_sources` | 100 | 含主资料的一组来源数量，允许 1–100000 |
| `max_object_bytes` | 32 MiB | 单提取对象，正整数 |
| `max_total_bytes` | 256 MiB | 组内提取载荷累计，正整数 |
| `max_decompressed_bytes` | 512 MiB | 累计解压数据，正整数 |
| `max_discovery_seconds` | 30 秒 | 累计发现与提取时间，有限正数 |
| 派生工作 `max_jobs` | 10000 | 函数与 API 均允许 1–10000 |
| 旧 PPT 内部记录遍历 | 深度 64 | 格式保护，区别于跨文档提取深度 |

补充依据：[派生工作](../ultrakb/application/pending.py#L164)、[API](../ultrakb/api_pending.py#L21)、
[PPT 解析](../ultrakb/pending/ppt_records.py#L114)。

## 调度 监听与界面刷新

| 限制 | 默认值 | 作用范围与覆盖方式 |
| --- | --- | --- |
| TaskManager 并行 worker | 2 | `max_workers` 构造参数允许 1–8；同库写入仍受知识库锁串行保护 |
| 单次提交执行请求数 | 1–10000 | 固定准入，不表示获得 10000 次模型调用额度 |
| 模型事件队列 | 128 项 | 单 worker 有界事件队列，溢出另行标记 |
| 临时回答文本缓冲 | 最后 1000000 字符 | 与已提交会话存储不同 |
| 写锁等待调度退避 | 0.25 秒起，最多 2 秒 | 让出进程槽后再试 |
| NativeWatch 防抖 / 扫描 / 核查间隔 | 2 / 1 / 30 秒 | 构造参数，正数 |
| NativeWatch 队列容量 | 256 | `capacity` 参数允许 1–10000 |
| NativeWatch 同时已提交任务 | 32 | `max_submitted` 参数允许 1–256 |
| CLI/API 监听事件历史 | 200 条 | `ULTRAKB_WATCH_MAX_EVENTS`，有界历史环 |
| API 监听 SSE 等待 | 300 秒 | `ULTRAKB_WATCH_SSE_TIMEOUT`，非导入时限 |
| GUI 本地读取线程 | 4 | 固定线程池，等待写锁时每 200ms 再试 |
| GUI 导入与任务详情刷新 | 250ms | 查看状态快照，不触发模型请求 |
| 桌面重复启动响应等待 | 5 秒 | 单实例握手，非业务超时 |
| `TaskManager.wait` 等待 | 60 秒 | 调用方函数缺省；超时抛异常，不自动终止任务 |
| 模型连接验证 | 20 秒、2 次发送、并发 1、重试 0、最多 2 轮、输出 ≤512 tokens | 仅合成连接测试；超时参数须大于 0 且不超过 60 秒 |

依据：[任务管理](../ultrakb/runtime/tasks.py#L62)、[队列](../ultrakb/runtime/tasks.py#L361)、
[监听](../ultrakb/runtime/watch.py#L108)、[事件环](../ultrakb/watch_service.py#L47)、
[SSE](../ultrakb/api_watch_events.py#L13)、[本地读取](../ultrakb/desktop/io.py#L28)、
[导入刷新](../ultrakb/desktop/import_progress.py#L38)、[详情](../ultrakb/desktop/task_details.py)、
[单实例](../ultrakb/desktop/instance.py#L45)、[连接验证](../ultrakb/application/model_connections.py#L292)。

## 渲染 产物与技能

| 限制 | 默认值或固定边界 | 作用范围 |
| --- | --- | --- |
| 数学或图表渲染源 | 100000 字符 | 单待渲染块 |
| 渲染子进程时限 | 60 秒 | 固定渲染时限 |
| 渲染栅格尺寸 | 宽高各 ≤8192 像素，面积 ≤33554432 像素 | Rust 渲染 helper 的分配保护 |
| Deck 文件 | 2 MiB | HTML 产物验证 |
| Deck 页数 | 至少 5 为硬检查；8–15 为建议范围 | 建议范围只产生警告 |
| 技能或 Deck 生成代理轮数 | 80；启用批评时 120 | 单次 Agents 运行 |
| Deck 批评 / 技能修订轮数 | 40 / 40 | 各自维护流程 |
| 技能评测并发 | 8 | 评测器并发，不等于模型执行器可同时发送 8 次 |
| 评测参考预览 | 4000 字节 | 输入参考预览 |
| 评测生成 / 单题评分 / 覆盖评分轮数 | 3 / 2 / 2 | 函数缺省参数 |
| 技能名称 / 描述 | 名称 ≤64 字符，描述 ≤1024 字符 | 描述少于 20 字符产生警告 |
| 技能主文件 / 单参考文件 | 50 / 100 KiB | 技能验证边界 |
| 发行清单读取 | 1 MiB | 发行元数据，非知识库资料 |
| 发行文件数量 | 最多 256 | 完整分发至少 5 项，分拆分发至少 2 项 |
| CLI 初始化语言 / 模型名称 | 50 / 100 字符 | 名称输入验证，不是模型上下文长度 |

依据：[渲染](../ultrakb/rendering/renderer.py#L80)、
[栅格尺寸](../ultrakb/rendering/rust-helper/src/main.rs#L87)、[Deck 验证](../ultrakb/deck/validator.py#L75)、
[生成](../ultrakb/agent/skill_runner.py#L46)、[批评](../ultrakb/deck/creator.py#L40)、
[技能修订](../ultrakb/application/skill_maintenance.py#L64)、[评测](../ultrakb/skill/evaluator.py#L53)、
[技能验证](../ultrakb/skill/validator.py#L40)、[发行元数据](../ultrakb/distribution.py#L44)、
[CLI 名称](../ultrakb/cli.py#L407)。

## 依赖独立默认值

ConDB 所带 YAML 的 `context_limit=100000`、`max_concurrent=10`、beam `max_turns=5`、
`select_k=1`，以及 block 检索的 16000/512 token 分块、4096 token 缓存预留、4 个检查点和
4 路块并发，是依赖独立配置。UltraKB 知识与会话入口明确选择 beam，传入共享执行器、
束宽 3、最多 8 轮、最多 6 个候选，并使用捕获容量计量；不能将依赖 YAML 当作
UltraKB 每次问答的实际上下文窗口。PageIndex 的独立默认模型也被捕获模型替换。

依据：[ConDB YAML](../vendor/ConDB/contextdb/config/config.yaml)、
[block](../vendor/ConDB/contextdb/config/retriever/block.yaml)、
[beam](../vendor/ConDB/contextdb/config/retriever/beam.yaml)、
[知识入口](../ultrakb/context/knowledge.py#L71)、[会话入口](../ultrakb/context/recall.py#L106)、
[索引入口](../ultrakb/indexer.py#L145)。

提供方特有的输出、速率、图像载荷与 SDK 参数随实际路由而变化。项目允许透传提供方配置，
不存在可以合并为一个数值的全提供方默认表；有效限制需结合执行快照、所装依赖与具体服务规则确定。
