# PaddleOCR 云任务：页码、分片与恢复契约研究

日期：2026-09-09。研究票：[Issue #17](https://github.com/wooveep/UrltraKB/issues/17)；已确认规格：[Issue #13](https://github.com/wooveep/UrltraKB/issues/13)。研究基线：`b5cdc0d4574bf4b402b71b367fad22794bc2604e`。状态：公开资料调查完成，待后续 HITL 选择和服务探针；**未实施业务代码，未通过真实服务验收**。

本轮仅阅读官方文档、官方 SDK 源码、公开发行信息及已确认设计；没有提交 OCR 任务、上传文件、调用模型、安装依赖或模型，也未使用 OCR 凭证。网络阅读遵循 agent-reach 的网页/Jina Reader 与 GitHub CLI 路由。本文不更改默认本地 OCR、显式选择云端、保留本地 PageIndex、彻底退出 PageIndex Cloud 且无旧兼容的既定边界。

## 结论

可以据公开资料设计**有界提交、已知任务恢复、分片下载检查点和明确的本地停止**；暂不能据此承诺 jobs JSONL 的输出顺序就是原 PDF 页序、响应丢失后可安全重提、远程取消成功或固定结果保留期。[jobs 指南][jobs]、[SDK 请求与查询][http]、[SDK 结果解析][parse]

推荐继续评估“**实际预切片 PDF + 持久输入页映射 + 独立结果映射验证 + 每片完整下载检查点**”。预切片能证明送出的子文件第几页来自原文哪页，却不能独自证明返回数组第几项对应那一页。结果映射无法验证时，应停止自动知识发布；可供 HITL 选择的保守替代是单页子文件或逐页人工核对，不能靠数组等长来放行。此为工程建议，依据已确认规格的原页证据和云完整性要求，以及 SDK 缺少显式页号映射验证的事实。[规格][spec]、[页对象][results]、[解析器][parse]

一个影响恢复的 SDK 细节必须处理：jobs 的 `12001 / HTTP 403` 表示每日页配额耗尽，但 Python SDK 会把 HTTP 403 分类为 `AuthError`；它抛出的业务错误也没有独立保存服务 `code`。适配层应保留 HTTP 状态、业务码、远端状态与脱敏消息，不能只继承 SDK 异常分类。[jobs 错误码][jobs-errors]、[SDK 错误处理][errors]

## 证据范围与可信等级

- **D：jobs 明文契约**——AI Studio 的异步 jobs 专用指南；可作为适配前提，但仍须验证目标部署响应。AI Studio 页面没有不可变版本链接，核对日期为本文日期。[jobs 指南][jobs]
- **S：SDK 实现事实**——PaddleOCR 官方仓库固定 commit `2661c7c0ef5c613e8f93c6e93b2e052399f0f854`（提交时间 2026-07-22）。下文所有 SDK/仓库文档引用固定到该 commit。客户端行为不能补成服务承诺。[固定 commit][upstream-commit]
- **X：相邻接口资料**——通用配额页、同步服务化或本地管线文档，只用于识别差异；不能把其数组、编码或限额语义自动移植到 jobs。
- **R：建议；U：未知**——设计候选及未被公开资料证明的命题，均不是已接受实现决定。

目标协议为 `https://paddleocr.aistudio-app.com/api/v2/ocr/jobs`，模型为 `PaddleOCR-VL-1.6`。专用指南的模型示例仍列到 VL-1.5；固定 commit 的官方 API 文档与 `Model` 枚举明确包含 VL-1.6，`parse_document` 默认选择它。公开最新发行 v3.7.0 的 tag commit `b03f46425e8ff4442b268ce449e3eef758146cd4` 也包含该枚举。故模型名有第一方依据，但**没有证明当前账户、部署和每个解析参数已可用**；本轮不选择或安装运行依赖。[jobs 模型表][jobs-submit]、[官方 API 模型文档][python-models]、[模型枚举][models]、[v3.7.0 发行][release]、[发行版枚举][release-models]

## jobs 契约证据表

| 主题 | 可证明内容与等级 | 不可推出的行为 / 后续处理 |
| --- | --- | --- |
| 提交 | **D/S**：`POST /api/v2/ocr/jobs`；文件采用 multipart，URL 采用 JSON；`file`/`fileUrl` 二选一，传 `model`、可选 `optionalPayload`、`pageRanges`、`batchId`；成功体含 `data.jobId`。[提交文档][jobs-submit]、[HTTP 源码][http] | HTTP 成功只证明请求结果，不等于 OCR 完成。非 JSON、丢响应或响应缺 jobId 不足以证明服务没创建任务。 |
| 查询 | **D/S**：`GET /jobs/{jobId}`；`GET /jobs/batch/{batchId}` 返回任务列表。[查询文档][jobs-query]、[批次文档][jobs-batch]、[HTTP 源码][http-query] | 不能把任务列表顺序当分片顺序。缺任务不证明任务从未创建；也没有按输入摘要找回 job 的公开接口。 |
| 状态 | **D**：`pending`、`running`、`done`、`failed`；`resultUrl` 在 done 时有效，`errorMsg` 在 failed 时有效。[状态表][jobs-query] | 未承诺每一中间状态都可观察、进度单调、严格状态转换时序或 SLA。`expired` 不是列出的任务状态，而是错误码。未知新状态应隔离处理，不当成功。 |
| 页数 | **D**：单请求最多 1000 页 PDF；页数超限为 `10006 / HTTP 400`。[限制与错误表][jobs] | 未说明上限检查发生在 `pageRanges` 选择前还是后。不能给 >1000 页原文件加小范围就假定合法。 |
| 字节数 | **D**：本地文件最大 50 MB，URL 文件最大 200 MB；超限 `10003 / HTTP 400`。[jobs 指南][jobs] | 未定义 MB 的精确字节换算、边界比较或 multipart 开销是否计入。规划应检查实际输出文件字节，保留裕量；不能按原文平均页大小估算通过。 |
| 范围语法 | **D**：`2,4-6` 表示第 2、4 至 6 页；`2--2` 表示第 2 页至倒数第 2 页。[范围字段][jobs-submit] | 没有证明乱序范围、重叠、重复、越界、空范围的归一化顺序和错误行为；也未定义进度页数是否按原文或选定集合计。首版候选仅生成已知总页数下的正整数规范范围。 |
| 批次 | **D**：`batchId` 用于批量查询；同一 batchId 最多创建 100 个任务，超限 `10009 / HTTP 400`。[批次字段][jobs-submit]、[错误表][jobs-errors] | 没有重复提交去重、幂等键、提交事务或请求摘要回显承诺。分片数量可超过 100，但需多个查询分组；批次不是恢复身份。 |
| 部分失败 | **D**：failed 不提供部分页成功结果；`11003` 可伴随 HTTP 200。[状态表][jobs-query]、[错误表][jobs-errors] | `extractedPages > 0` 不是可回收检查点。失败片整体未完成；可以保留其他已完整验证的片，但不能发布整份资料的部分知识。[规格][spec] |
| 结果位置 | **D/S**：done 返回 BOS 短链，含 `resultUrl.jsonUrl`，文档亦示例 `markdownUrl`。SDK 下载 JSONL 并读取每行 `result.layoutParsingResults`。[结果表与示例][jobs]、[JSONL 下载][http-jsonl]、[结果解析][parse] | JSONL 行数不是页数；每行允许数组。空 JSONL 或空数组经当前 parser 可得零页结果，因此“SDK 未抛错”不是完整性证明。 |
| 页对象 | **S**：`DocParsingPage` 保存 Markdown、图片、`pruned_result`、`input_image_url`、`raw` 等，没有显式的原页号字段。[页对象][results] | 不能声称底层 JSON 永远没有页字段；但公开 jobs schema 与 SDK 未保证其存在、基数或原文/分片/选择后的含义。 |
| 页顺序 | **S**：parser 按 JSONL 行序再按数组序追加页；示例 `page_num` 从 0 递增用于文件名；没有对范围或原页号校验。[parser][parse]、[示例][jobs] | 这是客户端遍历顺序，不是服务原页顺序保证。`len(pages)==预期数` 无法排除同长乱序、漏一页且重复另一页，或内容被过滤。 |
| 图片 | **D/S**：jobs 示例把 `markdown.images` 与 `outputImages` 的值作为 URL；SDK 保存器支持 HTTP(S) 下载。[jobs 示例][jobs]、[保存器][resources] | 不沿用同步 API 默认 Base64 处理，也不能直接使用远端名字作跨片唯一名。SDK 下载资源不验证所有 Markdown 引用闭合、不自动建立来源关系。 |
| 保留与过期 | **D**：`11001 / HTTP 404` 为 jobId 不存在，`11002 / HTTP 400` 为 job 已过期；结果被描述为短链。[错误表][jobs-errors]、[结果表][jobs-query] | 固定 job 保留期、JSONL/图片有效期、查询能否刷新短链、过期任务能否恢复结果均为 **U**。不能承诺隔日/长期恢复；404 不能一律写成“已过期”。 |
| 幂等与取消 | **U**：本次检查的 jobs 文档和 Python 请求/客户端没有公开幂等、cancel 或 delete 方法。[jobs][jobs]、[请求源码][http]、[客户端][client] | 结论是“未确认能力”，不是证明服务内部绝无该能力。TypeScript 的 `AbortSignal` 被接到本地 fetch；这不构成取消云 job 的 HTTP 调用。[TS HTTP][ts-abort] |

### 必须保留的错误差异

| jobs 返回 | 含义与工程建议 | 证据 |
| --- | --- | --- |
| `10001–10008 / HTTP 400` | 空文件、URL/大小/格式/内容/页数/模型/参数问题。保留具体码；同输入同参数不自动重复。修正后是新意图/尝试。 | [专用错误表][jobs-errors] |
| `10009 / HTTP 400` | 批次已有 100 个任务；调整后续分组，不重提已经有 jobId 的片。 | [专用错误表][jobs-errors] |
| `10010` | 队列满，文档建议稍后重试，但未列对应 HTTP 状态。收到明确拒绝结果才按受预算约束的拒绝处理；传输异常不能猜成此码。 | [专用错误表][jobs-errors] |
| `12001 / HTTP 403` | 每日页额度耗尽；属于配额阻塞，不等同凭证错误，不立即反复重试或静默换模型。 | [专用错误表][jobs-errors]、[规格][spec] |
| `12002 / HTTP 429` | 请求频率高；可按明确拒绝处理并有限退避。公开资料没给数值 QPS、Retry-After 或恢复时间承诺。 | [专用错误表][jobs-errors] |
| `11003 / HTTP 200`、`state=failed` | 业务解析失败；不能由 HTTP 200 或进度值报告成功。 | [专用错误表][jobs-errors] |
| 超时、连接中断、5xx、无 jobId 的异常响应 | **R**：若不能确认服务拒绝且未创建任务，提交应进入 `submission_unknown`；GET/下载故障则保留既有 jobId，在预算内恢复观察或下载。 | [POST 与 GET 分开的实现][http]、[规格的提交不确定要求][spec] |

通用配额页本次显示更新时间 2026-09-08，列每用户每模型每天 20000 页、超额 HTTP 429，并称 >1000 页仅处理前 1000 页。jobs 专用页给出的超页、日配额错误则是上述 `10006 / 400` 与 `12001 / 403`。**不能把通用页的截断行为、错误分类或每个账户的可用额度当成 jobs 已验证行为**；20000 页只能作为通用公开配额参考，不是本项目默认预算或 SLA。[通用配额页][quota]、[jobs 错误表][jobs-errors]

### SDK 可复用的能力与应补的边界

1. 使用“提交后返回 Job”与“用 jobId 等待/查状态”的分离能力，才能尽早落盘任务身份。高层 `parse_document` 把提交和等待串成一次调用，中断时调用者未必已拿到 jobId。[客户端 L102–163][client-submit]
2. Python 轮询实现从 3 秒间隔起，乘 1.5、上限 15 秒；默认轮询预算 600 秒，单 HTTP 请求默认 300 秒。它们是 SDK 默认值，不是服务推荐吞吐或本项目预算。[轮询][poller]、[客户端配置][client-config]
3. `poll_timeout` 不能当作完整调用的硬截止：提交发生在 poller 计时之前；每次 GET 与 done 后 JSONL 下载没有按剩余轮询期限缩短 HTTP 超时，下载后直接返回；资源保存另行执行。同步与异步 Python poller 均存在这个边界。建议统一执行控制用剩余总预算约束每次网络操作，停止后禁止新请求。[同步 poller][poller]、[异步 poller][async-poller]、[客户端][client-submit]
4. 当前 Python HTTP 包装与 poller 没有显式的应用级自动重试循环；轮询间隔增长是状态查询节奏，不是网络失败重试。不要据此声称所有底层网络行为恰好一次；若另加重试，需由项目唯一预算策略管理，提交异常和幂等 GET 分开。[HTTP][http]、[poller][poller]、[规格][spec]
5. parser 会合并每行 `dataInfo` 字典，后行同名键覆盖前行；没有验证页标识、重复、集合或资源。边界适配需保存可审计的行/项定位及原始响应证据，不能仅持久化展开后的 `pages` 与合并后的 `data_info`。[结果解析 L124–150][parse]

## 原页映射：输入可证明，不代表输出已证明

建议把两个关系分别保存：

- `input_page_map`：`(子文件摘要, 子文件物理页，统一为 1 起算) → (资料版本, 原文件物理页，1 起算)`。这个关系由本地 PDF 切分和回读验证产生，不依赖 OCR 文本、PDF 印刷页码或页标签。
- `result_page_binding`：`(jobId, JSONL 行, 数组项/稳定结果标识) → 子文件物理页`，另存 `mapping_method`、验证证据、状态。此关系需要独立验证；仅有输入清单不能补出缺失的输出身份。

例如送出原文 `[101, 102, 107]` 三页，输入映射可以确定为 `1→101, 2→102, 3→107`。返回三项时，`101,107,102` 与 `101,102,102` 都能通过长度检查；直接按数组下标映射会制造错误证据。若返回两项，也不知道缺的是哪页；在没有其他身份信号时应把**整个受影响片的结果映射**标为未确认，不能自行给末尾补缺页。这是从缺少页身份保证推导的反例，不是已观测到的服务故障。[页对象][results]、[解析器][parse]

邻近文档提供了尤其容易误用的线索：本地管线结果有 `page_index`；同一教程的**同步服务化 infer** 表描述数组按实际处理页依次返回，但明确说 `prunedResult` 去掉 `input_path` 与 `page_index`。这些话并没有定义异步 jobs 跨 JSONL 行、负数/不连续范围、过滤后的原页号。不能把本地字段或同步数组描述补成 jobs 契约，也不能断言 jobs 的 `prunedResult` 必然没有任何身份信息。[本地页字段][vl-page-index]、[同步响应表][vl-response]

**候选映射准入规则（R，待 HITL）：**

1. 最佳路径是目标 jobs 响应提供受文档或专项确认支持的页标识；验证其起算、所在坐标系、与分片/选择的关系、唯一性及完整覆盖，再合成原页映射。不能看到一个叫 `page_index` 的键就采用。
2. 若只有顺序，先用后述探针建立固定模型、参数与部署的能力记录；有限样本通过仍不是永久服务保证。生产自动放行需要可持续校验的身份依据，或经明确接受的顺序假设及严格门槛；如要求每次都能证明映射，应禁用此假设。
3. 单页子文件消除了“本次输入中的多个原页如何排序”的歧义；输入和 job 的绑定仍须可靠。零结果、多结果、空白页被省略或内容不足都不能仅凭“一页输入”视为成功；一项输出也不能证明文字和图表完整。无法判定时逐页检查或局部重处理。
4. 可得的 `inputImage`、页尺寸、图像特征或合成页标识可辅助交叉验证，但变换、重复/相似页面及 OCR 错误会引入歧义。无唯一匹配就保持未确认；不把图像 URL 文件名或 OCR 识别出的印刷页码当来源身份。
5. 任何缺页、重复页、无法解释的额外页或无身份乱序均阻止该片通过；所有需 OCR 的页与可复用原生解析页一起构成资料预期覆盖集合。映射通过后仍需逐页内容质量门槛。[规格][spec]

解析参数必须进入缓存/验证身份。SDK 含 `restructure_pages`、`merge_tables`、`markdown_ignore_labels`、`return_markdown_images` 等选项，公开相邻接口允许重构内容或不返回 Markdown 图片。首版建议冻结影响页结构与资产的选项；参数变化需重验映射与质量，不能只比结果项数。本轮未证明每个选项在 jobs/VL-1.6 的实际部署效果。[SDK 选项][vl-options]、[相邻服务参数][vl-service-options]

## 分片候选与推荐给 HITL 的取舍

| 候选 | 能解决什么 | 成本与准入门槛 |
| --- | --- | --- |
| **A：实际预切片 PDF（推荐评估）** | 同时控制每片物理页数和实际文件字节；本地保留不可变切片、摘要、输入页映射；失败只重做受影响片。连续片最易核对，稀疏 OCR 页可形成显式映射。 | 额外本地切片/存储成本；拆分可能复制共享资源，不能按平均页大小估算。多页输出绑定仍须验证，跨片表格关系交由后续结构/证据处理，不能假定云端跨 job 合并。 |
| **B：单页实际子文件** | 输入与唯一原页一一绑定，避免多页结果顺序推断，适合作为严格定位的保守路径或 A 的局部重处理。 | 请求数、批次与查询量显著增加；100 个 job 分组上限须处理；吞吐/额度未经实测。单页超过字节上限或内容质量未知仍会阻塞；不能宣称“一项返回”即可靠完整。 |
| **C：原文件 + pageRanges** | 原文件已在合法页数/字节内时，可选择少量页处理。 | 不减少需提供的原文件字节，未证明能绕过原文件页上限；范围排序、重复、负索引、进度与原页标识均待验证。每次 URL 读取还须绑定同一不可变文件；不作为超大 PDF 的默认解法。 |

以上是基于 [jobs 输入限制与范围字段][jobs-submit]、[jobs 批次限制][jobs-errors] 和 [规格][spec] 的设计建议，不是服务性能结论。

HITL 建议保留 A 为目标，以 B 作为“需要逐次可证定位而 jobs 缺少页身份”的候选保守路径；C 仅作为通过专项验证后的优化。**未决定**固定每片页数、字节裕量、并发数和默认预算；硬上限 1000 页不意味着应默认填满 1000 页。应由吞吐、失败重做成本、资源和配额探针共同确定。本轮没有触发这些探针。

分片规划应先保存原文版本，解析物理页数，再生成候选子文件，重新打开检查页数/顺序/渲染属性并测量实际字节；超限递归细分，单页仍超限则报告该页输入受限。缓存键包括切分器版本/参数、原文摘要与片摘要。不得为过限而静默降质、裁掉页面或换后端；需要有损处理时回到明确选择。[规格的版本化、云分片和质量要求][spec]

## manifest 与恢复状态候选

建议用每片独立尝试记录加资料级清单；无需引入新的通用任务调度产品。以下是字段候选，不是文件格式定稿或运行代码。[规格的云恢复与版本化要求][spec]

| 字段组 | 最小内容 / 作用 |
| --- | --- |
| 不可变输入 | 资料身份、资料版本、原文件摘要/物理页数；原生与 OCR 预期覆盖集合；子文件摘要/实际字节数/物理页数；本地持久子文件引用。 |
| 页选择与映射 | 原页集合、子文件选定集合、规范 `pageRanges` 或“全部”；`input_page_map`；结果行/项定位、`result_page_binding`、映射方法/证据/验证状态。 |
| 处理身份 | 云后端、端点、显式模型、实际序列化参数及其摘要、适配器/SDK 版本；服务版本回显若可得则保存，缺失不得伪造。凭证不进入清单。 |
| 提交意图 | 本地唯一 `attempt_id`、意图持久化时间、请求指纹、可选 batchId、提交状态、已知 jobId、最后业务码/HTTP 状态/脱敏诊断。batchId 与请求指纹都不是服务幂等键。 |
| 观察与预算 | 本地停止/暂停原因、最近远端状态及时间、累计尝试/等待/下载使用量、本次剩余预算；保留未知提交窗口和后续人工决定。 |
| 结果和资产 | 结果获取批次/时间、受管理的 JSONL 证据引用及摘要、逐页/资产下载状态、大小/摘要/校验结果、引用改写表；不能仅存远端短链当永久资产。 |
| 完成检查点 | 输入/配置校验、页集合/映射校验、正文质量、必需资产引用闭合、落盘验证均通过后产生的不可变解析产物身份。 |

原始 JSONL 可能包含签名短链，应作为受控暂存证据处理，不进入公开报告或普通诊断；持久结果应改写为受管理资产引用。manifest 不保存访问令牌；短链只作敏感、可失效的下载定位，不能成为引用或恢复承诺。必需图像按片/结果/内容身份管理，同名图片不能跨片覆盖；重命名或去重后同步改写 Markdown 引用。[jobs 短链与图片示例][jobs]、[资源保存器][resources]、[规格][spec]

推荐把**本地控制状态、远端最后观测状态、下载校验状态分开**，以免一个 `done/stopped` 字段丢掉恢复依据：

| 本地状态候选 | 进入条件 | 恢复动作与禁止事项 |
| --- | --- | --- |
| `planned` | 片文件与输入清单校验完成，尚未持久提交意图 | 在新预算内重新校验输入，原子持久意图后才能开始一次 POST。 |
| `submitting` | 意图已持久，提交可能正在进行 | 启动恢复时只要不能证明尚未发送，就转 `submission_unknown`。即使崩溃发生在实际发出前，也不能由此自动猜测“未提交”。 |
| `submission_unknown` | 请求超时/断开/异常响应，或服务已应答但 jobId 尚未可靠落盘 | 不盲重提。保留尝试和范围；可只读核对候选 batch 查询/服务支持信息，但公开返回缺输入摘要且无完整性/一致性承诺，列表空或只有一项都不足以普遍自动认领。只有可靠关联到确切 jobId 才继续，否则需明确接受可能重复任务的新提交。 |
| `submitted` / `observing` | jobId 已原子落盘；最近状态为 pending/running 或尚未查 | 先查该 job；不重新 POST。观察超时/断网保留 jobId、累计预算与最后状态。 |
| `remote_done` | 查询到 done 及结果定位 | 只表明远端完成；下载 JSONL 和必需资产，校验绑定、集合与内容。缺链接/结构异常保持未完成。 |
| `downloading` / `validating` | 已有部分本地结果 | 已完整校验的文件可按摘要复用；不完整文件重新下载到临时路径后原子替换。未证明远端支持 Range/稳定 ETag，不能直接追加旧残片。 |
| `checkpoint_valid` | 该片所有必要产物落盘且校验通过 | 继续时验证依赖身份，复用该片；其他片失败不删除它。整份资料仍需所有必要片和原生页覆盖及全局门槛通过。 |
| `remote_failed` | failed 或 11003 | 不使用进度中的“成功页”。保留其他片检查点；对本片给出原因和范围。重新处理产生新 attempt，不能隐藏旧失败记录。 |
| `job_expired` / `result_unavailable` | 明确 11002；或短链失效/资源无法获取 | 区分任务过期、资源链接失败、404 未找到、认证/端点错误。已落盘有效检查点可继续使用；否则先有限重查已知 job 获取定位，刷新能力未知。仍不可得则列出受影响片/资产，重新 OCR 需新的明确意图。 |
| 本地 `stopped`（独立维度） | 本地停止已确认，禁止新请求与迟到发布 | 展示“已停止等待，云任务可能仍在运行”；保留远端状态与 jobId。停止发生在 POST 未知窗口时还要保留 `submission_unknown`。继续后查询既有任务；不能把停止报成云端取消或零后续配额消耗。 |

状态名为建议；依据是 [jobs 可见状态与失败契约][jobs-query]、[过期错误][jobs-errors]、[SDK 分离提交/等待能力][client-submit] 和 [已确认规格][spec]。`checkpoint_valid` 必须由本地验证产生，不直接映射服务 done。页映射检查失败与内容质量未确认可作为独立原因字段，避免增加互相覆盖的终态。

## 后续最小探针：仅描述，全部未执行

应使用新生成的无用户内容 PDF，文件摘要固定，每页含不依赖印刷页码的唯一大字标识、非文本图案与已知页尺寸。配备纯文本、扫描、空白、仅插图、两张视觉相似页、旋转页及跨页表格。人为标识用于核验探针输出，不进入生产资料，不单凭 OCR 识别标识判定映射成功。需记录端点、模型、完整解析选项、SDK/适配器版本、HTTP/业务码和实际原始响应形状；服务版本未回显则注明未知。[规格的合成/真实边界验收要求][spec]

| 探针 | 最小命题与样本形状 | 验收/阻断意义 |
| --- | --- | --- |
| P1 页身份与范围 | 8 页，先全量，再 `2,4-6`，再 `2--2`；对一个含原页 `[2,4,5,6]` 的实际子文件再选子页 `2-3`。观测原始每行、每项、raw/prunedResult/dataInfo/inputImage 的身份字段、起算与坐标系。 | 证明目标部署是否存在可用身份信号；任何缺失/重复/映射歧义均不能启用多页自动精确引用。 |
| P2 顺序与过滤 | 同样 8 页，另测乱序 `6,2,4-5`、重叠 `2,2,4-5`、越界与空选择；含空白、仅插图、相似页。只对拟支持的语法建立能力记录。 | 区分按原页排序、请求顺序、去重、报错和静默忽略；验证空白是否有显式占位。未支持的复杂语法由本地拒绝，不靠猜测处理。 |
| P3 限制 | 稀疏合成 PDF 的 1000/1001 页；对 1001 页原文件仅选 1 页；分别构造本地和 URL 文件大小边界两侧。大边界探针按配额另行批准，可优先向供应商求证。 | 确认页上限应用于原文件还是选择集、字节换算/边界和超限错误；没有结果前采用实际预切片与裕量。不能以超限测试会提前拒绝为由无预算运行。 |
| P4 提交响应丢失与恢复 | 一份小片；本地故障注入：意图落盘后、发出前、服务接收后丢响应、jobId 返回但持久化前、jobId 落盘后逐点中断。真实提交后的断响应需后续明确授权；控制流先可用可控 Adapter 验证。 | 必须停在正确的不确定窗口，有 jobId 就不重提。批次查询只作为核对线索；相同 batchId 去重不能由单次现象认定为幂等保证。 |
| P5 done 与下载 | 两片均 done，分别在 JSONL、单个必需图片、校验后落检查点前中断；注入缺/重/乱序页、截断 JSONL、同名不同图、HTML 错误页及损坏图片。 | 这些主要是本地 Adapter 故障测试，无需制造真实服务故障。done 未落齐不得成功；续传不重复 OCR；引用不覆盖、不失联。 |
| P6 失败与停止 | 两片中一片模拟 failed，另一片有完整检查点；停止本地轮询后再收到 done。远端任务与必要资产可用时再继续。 | failed 无部分片检查点，已验证片仍保留；停止后无新请求/晚到知识写入；UI 明确远程可能继续，不伪报 cancel。 |
| P7 保留期与短链 | 少量小样本，在记录时刻重新 GET 已知 job 与旧/新 JSONL、图片定位；确认任务和资产不同寿命、GET 是否刷新链接。不得为本研究启动持续监控。 | 实测只给下界/观测值，不能推出保证保留期。承诺具体恢复窗口需供应商明确文档或服务约定。 |
| P8 错误和预算 | Adapter 注入 `12001/403`、`12002/429`、`11003/200`、未知状态，以及 GET/下载慢于剩余总预算的行为；另行授权后小样本验证目标部署响应。 | 日额度与认证不同；业务失败与 HTTP 成功不同；停止/超时不进入自动重提；总预算覆盖下载而非只计 sleep。 |

P1–P3 的生产相关服务行为需要真实服务或供应商说明；P4–P6、P8 的本地状态/异常门槛首先可用受控 Adapter 证明。两类证据不能互相替代。P7 不存在可由有限实验“证明永久有效”的通过条件。

## 上线前必须关闭的未知与决策

1. **原页映射门槛**：目标 jobs/VL-1.6 是否提供可用页身份，复杂范围、空白页和输出分段的具体语义。不能关闭时，由 HITL 在单页路径、逐页确认或禁用多页云自动发布之间选择；不能以等长数组替代。
2. **配置与限额门槛**：目标部署确实接受 VL-1.6 与选定参数；明确页/字节边界，或以经验证保守子文件策略规避。固定分片大小、并发和预算需另有测量依据。
3. **恢复门槛**：提交不确定、已知 jobId、done 未下载、单片失败、链接不可用、停止与迟到响应的状态演练全部通过；幂等性未知不阻止采用保守状态机，但阻止“自动保证不重单”的宣传。
4. **本地产物门槛**：JSONL 可解析、原页集合及绑定可验证、必需资产可读且引用闭合、逐页质量已通过或已人工确认，才允许整份资料后续发布。
5. **承诺边界**：供应商没有承诺的取消能力和保留期不必须凭空补齐；产品按已接受规格明确报告“仅停止本地等待”“远端/短链可能过期”。若未来要承诺远端取消或确定恢复时长，必须取得新的官方契约证据。[规格][spec]、[jobs][jobs]

HITL 仍待选择分片/映射准入方案及后续探针预算；本文完成的是调查与可审查建议，研究票完成不等于授权实施、上传或付费调用。

[spec]: https://github.com/wooveep/UrltraKB/issues/13
[jobs]: https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5
[jobs-submit]: https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5#%E6%8F%90%E4%BA%A4%E8%A7%A3%E6%9E%90%E4%BB%BB%E5%8A%A1
[jobs-query]: https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5#%E8%8E%B7%E5%8F%96%E8%A7%A3%E6%9E%90%E7%BB%93%E6%9E%9C
[jobs-batch]: https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5#%E6%89%B9%E9%87%8F%E8%8E%B7%E5%8F%96%E4%BB%BB%E5%8A%A1%E7%BB%93%E6%9E%9C
[jobs-errors]: https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5#%E9%94%99%E8%AF%AF%E7%A0%81
[quota]: https://ai.baidu.com/ai-doc/AISTUDIO/Xmjclapam
[upstream-commit]: https://github.com/PaddlePaddle/PaddleOCR/commit/2661c7c0ef5c613e8f93c6e93b2e052399f0f854
[release]: https://github.com/PaddlePaddle/PaddleOCR/releases/tag/v3.7.0
[release-models]: https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/paddleocr/_api_client/models.py#L23-L30
[models]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/models.py#L23-L47
[vl-options]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/models.py#L129-L165
[http]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/_http.py#L75-L183
[http-query]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/_http.py#L159-L183
[http-jsonl]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/_http.py#L185-L203
[client]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/client.py
[client-config]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/client.py#L48-L70
[client-submit]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/client.py#L102-L163
[errors]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/_core.py#L148-L171
[results]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/results.py#L28-L51
[parse]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/_poller.py#L124-L152
[poller]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/_poller.py#L38-L86
[async-poller]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/_async_poller.py#L30-L78
[resources]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/paddleocr/_api_client/_resources.py#L27-L119
[python-models]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/docs/version3.x/inference_deployment/serving/paddleocr_official_api/python.md#L54-L61
[ts-abort]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/api_sdk/typescript/src/internal/http.ts#L178-L225
[vl-page-index]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/docs/version3.x/pipeline_usage/PaddleOCR-VL.md#L1378
[vl-response]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/docs/version3.x/pipeline_usage/PaddleOCR-VL.md#L2283-L2325
[vl-service-options]: https://github.com/PaddlePaddle/PaddleOCR/blob/2661c7c0ef5c613e8f93c6e93b2e052399f0f854/docs/version3.x/pipeline_usage/PaddleOCR-VL.md#L2223-L2245
