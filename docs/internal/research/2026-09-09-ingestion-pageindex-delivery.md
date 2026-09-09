# PageIndex 阻塞、重试与生命周期：可复现交付路径

研究日期：2026-09-09。对应研究票 [研究：PageIndex 阻塞、重试与生命周期修复有哪些可复现交付路径？](https://github.com/wooveep/UrltraKB/issues/15)，继承已确认规格 [Spec: 大型 DOCX/PDF 可靠导入、PaddleOCR 与证据编译（三批设计）](https://github.com/wooveep/UrltraKB/issues/13)。本轮只读调查并发布报告；未安装依赖或模型，未执行真实 LLM/OCR 请求、上传资料或运行测试套件，未实施运行时代码。研究完成不代表运行性能、取消时延或平台兼容已验收。

## 结论与推荐

**推荐由 OpenKB 执行控制拥有预算、尝试、停止和提交权限，配合一个范围受限、来源可追溯的 PageIndex 补丁包。** 参数传递、显式本地入口、存储所有权可使用当前官方接口；实际 PDF 目录解析里的同步阻塞、同步并发许可绕过、硬编码异常重试和取消清理，不能仅靠这些接口修好。固定源码 fork 是补丁的维护来源，带唯一版本和 SHA-256 的 wheel 是优先分发形式。先不为 LiteLLM/OpenAI 另建 fork：现有重试参数、客户端接口和清理入口足以开始接入；只有离线探针证明指定 provider 路径无法满足契约，再扩大补丁面。此为基于下列源码的候选建议，尚未采纳，交由下一张人工决策票选择；尚未完成补丁或安装验证。[PageIndex 管线][pi-pipeline]、[调用包装][pi-utils]、[LiteLLM 客户端][ll-openai]

保留本地 PageIndex 作为导航增强、移除 PageIndex Cloud、完整内容独立于导航，以及第一批就落实预算和停止，均为已接受决定。本报告不重开这些产品选择，也不以“先跳过所有 PDF 目录解析”代替第一批阻塞修复。[Spec: 大型 DOCX/PDF 可靠导入、PaddleOCR 与证据编译（三批设计）](https://github.com/wooveep/UrltraKB/issues/13)

## 1. 版本、来源与证据分层

OpenKB 研究基线为 `b5cdc0d4574bf4b402b71b367fad22794bc2604e`，报告分支从该提交建立。`pyproject.toml` 精确锁定以下版本；本机安装元数据一致。通过 `gh api` 读取官方 tag 的对象并解引用，得到下列不可变提交；未把当前官网或主分支的新功能视作锁定版本能力。[依赖声明][kb-deps]、[锁文件][kb-lock]

| 组件 | 锁定／本机版本 | 官方源码提交 | 锁文件中的 wheel SHA-256 |
| --- | --- | --- | --- |
| PageIndex | `0.3.0.dev3` | [`9ad54122bbd519cec8913198e2d63cff92781c1e`](https://github.com/VectifyAI/PageIndex/commit/9ad54122bbd519cec8913198e2d63cff92781c1e)；tag 为带注释的 `v0.3.0.dev3` | `5056969108785f5c9c31e03ce60f5fa1043be96ecffb5a06353375212027b258` |
| LiteLLM | `1.87.2` | [`1296275dc52d9f4e05696380735037fbb841fcc3`](https://github.com/BerriAI/litellm/commit/1296275dc52d9f4e05696380735037fbb841fcc3) | `d2161fc0ce66f800c69fd522cb73f03683edd164fc668e00fc3f23e1f6b4e6bf` |
| OpenAI Python | `2.44.0` | [`6d9262d5c666a1e4d47f63178db907ba3087ac5d`](https://github.com/openai/openai-python/commit/6d9262d5c666a1e4d47f63178db907ba3087ac5d) | `0a2a3ab2e29aeda368700f662ff9ba0f9df17ba4c54577a64e08b8115a3cc0ad` |

另核对官方源文件与本机文件逐字节相等：PageIndex 的 `config.py`、`client.py`、`backend/local.py`、`index/{pipeline,utils,page_index}.py`；LiteLLM 的 `main.py`、`utils.py`、`llms/openai/openai.py`、`litellm_core_utils/logging_worker.py`、`llms/custom_httpx/async_client_cleanup.py`；OpenAI 的 `_base_client.py`。这是关键源码一致性检查，**不是对整个已安装环境或发行物重新验签**。wheel 哈希来自基线锁文件，未在本轮下载并重新计算 wheel。

主工作区存在未提交诊断／取消改动。只读检查确认 `openkb/cancellation.py`、`runtime/model_cancellation.py`、`runtime/diagnostics.py` 尚未跟踪；`indexer.py` 有 SQLite 所有权改动。报告的固定基线链接不包含这些内容；它们不随本报告发布。为区分观察时点，取消文件的 SHA-256 分别为 `8569008f8f2495abecb1815d3dd5e6228c76a5c8e10b06553b35b8d77c1e9f29`、`3acdd1208034d7d58967f0d6fb0c8ac80b0d66f1f376f33333d2f38ddc5968f4`。主仓定位代码前先使用 CodeGraph；基线工作树无索引，未建立新索引。

下文“源码事实”指所列固定提交或明确标注的本地未提交快照；“推断／建议”表示由控制流得出的风险或工程选择；“待探针”表示本轮没有执行的行为验证。既有设计中的离线复现仅作为先前记录，不冒充本轮复跑，也不冒充用户长 PDF 的现场复现。[Spec: 大型 DOCX/PDF 可靠导入、PaddleOCR 与证据编译（三批设计）](https://github.com/wooveep/UrltraKB/issues/13)

## 2. 真实调用链与阻塞位置

### 2.1 当前 PDF 路径

```text
OpenKB index_long_document
  → PageIndexClient(...).collection().add(PDF)
  → LocalBackend.add_document
  → PdfParser.parse → 无 level 的逐页 ContentNode
  → index.pipeline.build_index
      → _run_async(_content_based_pipeline)
          → await tree_parser
              → check_toc                         [同步]
              → await meta_processor
                  → process_toc_with_page_numbers [同步]
                    / process_toc_no_page_numbers [同步]
                    / process_no_toc              [同步]
                  → await verify_toc / 修正 / 回退
              → gather 递归展开大节点
      → _run_async(generate_summaries_for_structure)
      → generate_doc_description                  [同步 build_index 内]
  → 保存 PageIndex 文档
  → OpenKB 读取结构、提取页内容并写受事务管理的产物
```

链路来自 [OpenKB indexer][kb-indexer]、[Collection][pi-collection]、[LocalBackend][pi-local]、[PDF parser][pi-pdf]、[pipeline][pi-pipeline] 和 [tree_parser/meta_processor][pi-tree]。这些同步 TOC 帮助函数最终调用同步 `llm_completion`；它本身会同步等待模型并在异常时 `time.sleep`。

`_run_async` 在无运行中循环时调用 `asyncio.run`；已有循环时新建 `ThreadPoolExecutor`，复制 `ContextVar` 上下文，在新线程 `asyncio.run`，但调用者用 `.result()` 同步等结果。PDF 树解析和节点摘要分别经过一次 `_run_async`，因此不是一个贯穿索引全程的长寿命事件循环。**把整个 `col.add` 放到工作线程，只能释放外部调用者的循环；内部 `tree_parser/meta_processor` 的循环仍会被同步 TOC 工作占用。** 文档描述在该实际入口的同步 `build_index` 中执行，不能套用文件后部旧 `page_index_main/page_index_builder` 的结论。[pipeline][pi-pipeline]、[目录算法][pi-tree]

推断：可优先把异步目录算法里的同步帮助函数改为可等待的执行边界，或逐步转换为原生异步。线程方案必须传播配置／取消上下文，并承认“停止等待”不会自动终止已经运行的线程。只在最底层替换 `litellm.completion`，若上层仍用同步 `done.wait` 或 `.result()`，依然阻塞该调用线程。[Python to_thread](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.to_thread)、[Executor 取消与退出](https://docs.python.org/3.12/library/concurrent.futures.html#concurrent.futures.Executor.shutdown)

### 2.2 配置扩展点已存在，但 OpenKB 尚未接全

`IndexConfig` 已声明 `llm_params`、`max_concurrency`，未知字段禁止传入。`build_index` 用 `llm_params_scope`、`max_concurrency_scope` 设置 `ContextVar`；每次叶调用用 `get_llm_params()` 合并全局默认和当前索引覆盖。`_run_async` 的新线程分支已复制上下文。`model/messages` 是保留字段，模型由 `LocalClient(model=...)`／`IndexConfig.model` 指定；不应塞进 `llm_params`。[配置源码][pi-config]、[pipeline][pi-pipeline]

基线 `_build_index_config` 只传三个增强开关与并发值，未传 `llm_params`。compiler 显式读取 OpenKB 的 timeout／headers／bundle，不能据此断言 PageIndex 收到相同有效配置。PageIndex 自身默认 timeout 为 120，环境覆盖在导入时读取；非正数变成 `None`，且未专门拒绝无穷值。这是**依赖当前默认的事实，不是推荐预算**。OpenKB 应在边界解析出有限正值及输出限制，按有效配置快照逐索引传入；不要靠对 PageIndex 全局 `set_llm_params` 的临时修改隔离并发任务。[OpenKB indexer][kb-indexer]、[compiler][kb-compiler]、[PageIndex 配置][pi-config]

显式 `LocalClient` 是锁定版本现有公开入口。精确地说，该版 `PageIndexClient.__init__` 依据传入 `api_key` 是否为 `None` 选后端，并未在构造器自行读取 `PAGEINDEX_API_KEY`；**OpenKB 基线读取环境后把 key 传进去**。退出旧云功能应删除应用端这条接入，并改用 `LocalClient` 使意图可核验，而不是把“SDK 会自动读旧环境变量”写成该版本的事实。[客户端][pi-client]、[OpenKB indexer][kb-indexer]

### 2.3 并发限制并非严格上限

异步叶调用使用跨线程的进程 `threading.Semaphore`，可再用作用域许可收紧；等待以非阻塞 acquire 加 `asyncio.sleep` 进行。索引的 `max_concurrency` 只能收紧进程默认上限，不能扩大；进程默认可另设，不能把请求值直接当实际并发。[调用包装][pi-utils]、[作用域][pi-config]

同步 `_sync_llm_semaphore` 检测到运行中事件循环时，尝试非阻塞 acquire；拿不到进程或作用域许可仍执行 `yield`，继续请求。这是避免把同循环的许可持有者锁死的权宜实现，**不能承诺绝不超过并发限额**。源码注释给出“每循环可能多一个”的解释，不应将其推广成跨 worker 的全局严格上限。修复需要先移走同步循环阻塞，再确保同步／异步请求都在真正取得许可、预算预留和停止复查后发出。许可等待也计入阶段／处理项耗时。[同步许可源码][pi-utils]

## 3. 重试、取消和收尾边界

| 层级 | 锁定源码事实 | 应交付的控制边界 |
| --- | --- | --- |
| OpenKB 索引 | `col.add` 最多尝试 3 次，普通 `Exception` 均可能重跑整次索引 | 不把认证／参数／预算耗尽当随机目录质量问题；由统一策略决定是否重试 |
| PageIndex 模型叶调用 | sync/async 各硬编码最多 10 次尝试，捕获普通 `Exception`；间隔分别同步／异步等待 1 秒；用尽后返回空串或 `("", "error")` | 可关闭内部自动尝试；错误保持类型与原因，不能让空结果冒充必要阶段已完成 |
| PageIndex 目录算法 | TOC 模式回退、错误页修正和递归大节点展开还能产生新逻辑调用；部分 gather 错误降为叶节点 | 分开记录算法回退与同一请求重试；纳入导航预算，保留降级原因 |
| LiteLLM wrapper | `_get_wrapper_num_retries` 读取每次参数、全局默认及 `retry_policy`；`main.py` 中 `num_retries` 可覆盖 `max_retries` | 每次显式收敛两项及策略／fallback；对实际 provider 分支核验最终 SDK 设置 |
| OpenAI SDK | SDK 默认 `max_retries=2`，`0` 可禁用；LiteLLM 构造或更新客户端时另传 `max_retries` | 不把 SDK 自身默认直接当经 LiteLLM 后的有效值；检查构造结果和传输尝试 |
| OpenKB 编译 | `_run_compile_with_retry` 最多运行 2 次，普通异常首次后等待 2 秒；此层在索引后的编译分支，不是所有索引调用外面再乘一个 2 | 消除整篇盲重跑；可恢复阶段与预算核算由共享执行控制决定 |

来源：[OpenKB 索引][kb-indexer]、[编译包装][kb-documents]、[PageIndex 叶调用][pi-utils]、[TOC 算法][pi-tree]、[LiteLLM wrapper][ll-utils]、[LiteLLM main][ll-main]、[OpenAI 客户端][oa-client]、[SDK 默认][oa-constants]。

这里的 3／10／2 是代码循环或 SDK 默认，**不能相乘为用户现场 HTTP 请求数**：索引有不同数量的逻辑调用，错误可能在传输前发生，缓存可能命中，某层可能返回空结果，provider 与配置也会改变内部重试。`num_retries=0,max_retries=0` 是 LiteLLM/OpenAI 路径的候选收敛参数，不会关闭 PageIndex 那个硬编码循环；也不能替代其他 provider 的最终构造与传输探针。[PageIndex][pi-utils]、[LiteLLM 参数映射][ll-main]、[OpenAI 路径][ll-openai]

### 3.1 未提交取消代码的实际风险

本地未提交 `OperationCancelled` 继承 `BaseException`，可绕过普通 `except Exception`，方向上避免把停止当模型失败。但基线 compiler 的 `gather(return_exceptions=True)` 结果只检查 `isinstance(r, Exception)`，随后解包普通结果；加入该取消类型后可能被当作元组解包并产生 `TypeError`，继而被编译重试包装捕获。**本轮只核对控制流，没有重跑完整取消实验。** 既有规格记录的离线复现是“多等一次退避，下一次尝试被停止检查拦截，没有新模型请求，最终仍抛取消”，不能升级为“取消一定导致多次远程请求或最终失败”。[compiler][kb-compiler]、[编译包装][kb-documents]、[Spec: 大型 DOCX/PDF 可靠导入、PaddleOCR 与证据编译（三批设计）](https://github.com/wooveep/UrltraKB/issues/13)

同样需要覆盖依赖内部的 `gather`：PageIndex 递归展开结果也只识别 `Exception`，可能忽略 `BaseException` 值；`LocalBackend.add_document` 先复制文件，再在 `except Exception` 清理，停止类型可跳过这一局部清理。**这是异常分类与所有权缺口，不是已经测得的文件或 socket 泄漏数量。** 可用依赖补丁显式传播取消并完成清理，或在 OpenKB 的私有索引暂存中统一回收；无论选择哪种，都要在有写入能力的执行体退出后再回滚／释放锁。[PageIndex gather][pi-tree]、[本地存储流程][pi-local]

本地未提交同步包装虽然把 SDK 请求放入 daemon 线程，调用方仍 `done.wait` 轮询；在异步流程中调用它仍占用事件循环。异步包装调用 `cancel()` 后又 `gather` 等待收尾，这比直接丢弃请求更合理，但代码注释不能证明所有 provider 都在有限时间关闭传输。`asyncio.wait_for` 本身也会等待实际取消完成，总等待可超过指定 timeout。[Python 超时／取消语义](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.wait_for)

### 3.2 日志超时、客户端与进程监督分别负责什么

LiteLLM `LoggingWorker._process_log_task` 对日志协程使用 `asyncio.wait_for(timeout=self.timeout)`，异常以 `LoggingWorker error` 记录。它有队列、`flush()`、`stop()` 和退出处理；循环改变时会重建状态。由此可确认日志回调有独立超时边界，**不能仅凭该堆栈的 `TimeoutError` 判为模型请求网络超时**。循环被同步工作占用会使健康回调无法及时调度；这是一种机制解释，具体现场仍需模型传输跨度、循环延迟及日志跨度共同确认。不能用提高日志 timeout 掩盖阻塞，也不能删除真正慢日志回调的告警。[LoggingWorker][ll-logging]

PageIndex `SQLiteStorage` 已有 context manager 和 `close()`，关闭所有登记连接；OpenKB 可通过 `storage=` 明确拥有它。主工作区新增的 ExitStack 处理的是 SQLite 关闭顺序，不是模型连接池收尾。PageIndex 当前 Client 没有统一暴露索引模型客户端和日志生命周期；`build_index` 也没有对应整体清理步骤。[SQLiteStorage][pi-storage]、[客户端][pi-client]、[pipeline][pi-pipeline]

LiteLLM 导出 `close_litellm_async_clients()`，但具体实现按缓存对象形状尝试关闭异步 transport/client，并吞掉清理异常；它不等价于“所有 provider 的同步和异步 SDK 客户端已确认关闭”。OpenAI 提供同步 `close()` 与异步 `await close()`／上下文管理。最稳妥的集成是跟踪当前执行拥有的客户端，在仍有效的所属循环完成有期限收尾，分别记录关闭成功、失败或未知；不要在并发 REST 线程里全局清空其他任务共享缓存。[LiteLLM 清理实现][ll-cleanup]、[OpenAI close][oa-client]

| 能力 | OpenKB 进程监督可承担 | 仍需内部适配／补丁或明确验收 |
| --- | --- | --- |
| 用户停止、阶段／总预算到期 | 停止发新工作、撤销结果采用权，超出收尾边界后终止隔离执行体，确认退出，再恢复事务与释放锁 | 停止检查抵达排队／在途／回退，取消不被 gather 吃掉；普通完成也要收尾 |
| 任意同步库或解析卡住 | 进程作为最终可终止边界；线程无法可靠强制取消 | 事件循环健康、严格并发、请求计数、及时协作式停止不能靠最终杀进程代替 |
| 正式 Wiki 不被迟到结果修改 | 请求线程只返回数据；正式写入由拥有任务与事务的执行控制授权；退出确认前保持所有权 | PageIndex 本地 blob／DB 也有写能力，不能把整个 `col.add` 线程当“仅网络线程”放弃 |
| SDK 日志与网络连接收尾 | 限时等待并记录辅助告警，最后回收进程 | 进程消失不是 SDK 正常关闭已验证；无法承诺远端已停止运算或计费 |

上表是落实 [Spec: 大型 DOCX/PDF 可靠导入、PaddleOCR 与证据编译（三批设计）](https://github.com/wooveep/UrltraKB/issues/13) 的建议。Python 文档明确 `Future.cancel` 不会取消已运行的线程，executor 上下文会等待线程结束；因此需要监督进程作为最终边界，而不是把线程 timeout 当安全退出。[Python futures](https://docs.python.org/3.12/library/concurrent.futures.html#concurrent.futures.Future.cancel)

## 4. 候选交付矩阵

| 路径 | 可以负责 | 无法单独解决／代价 | 可复现交付方式与判断 |
| --- | --- | --- | --- |
| A. OpenKB Adapter＋官方接口，保留原 wheel | `LocalClient`、`IndexConfig.llm_params`、模型配置、`storage=`、任务监督与结果契约 | 硬编码十次循环、内部同步 TOC 与许可绕过仍在；不是完整第一批 | 保持现有三个精确版本与锁哈希，提交 Adapter；**必做的基础层** |
| B. 官方自定义 parser／确定性 level 管线 | `register_parser` 注册优先级更高的 parser，产出有 `level` 的 `ContentNode` 时直接构树，避开内容推断目录 | 节点摘要／描述仍可能请求模型；需要可靠标题与位置，改变 PDF 导航路径；不能替代原有回退的修复验收 | 原 wheel＋版本化 parser／解析产物；**可用于后续导航增强／降级，不作为第一批唯一修复** |
| C. OpenKB 内版本限定的运行时适配／函数替换 | 在隔离 worker 中替换完整同步／异步请求边界、内部帮助函数或 pipeline | 私有接口和导入别名易漏覆盖；修改一个 `utils` 名称未必覆盖已经导入的引用；跨版本审查成本高；纯 SDK 包装消不掉上层同步等待 | 适配源码随 OpenKB 发布，启动校验依赖版本／预期接口，不修改 site-packages；**短期桥接可行，须同样通过真实控制流验收** |
| D. 固定 PageIndex 补丁 wheel | 清晰修补 TOC 异步边界、严格许可、可关闭内部重试、取消／空结果契约、生命周期入口 | 需要维护小补丁集、打包／许可证／平台与上游合并检查；不能解决 OpenKB 自己的编译重试和提交所有权 | 上游精确提交＋审查后的补丁提交＋独立版本＋wheel SHA-256＋构建来源；**推荐交付形式** |
| E. 固定源码 fork 直接安装 | 与 D 相同，便于复核 diff 与向上游贡献 | 完整 commit 固定源码却不固定构建工具／产物；终端安装仍需构建条件 | 依赖指向完整 commit，锁运行与构建依赖；**适合开发核验，发布时优先由该提交构建 D** |
| F. 无界升级／移动 branch／手改 site-packages | 能快速试想法 | 无法可靠重装、回退或确认谁运行了什么版本；可能引入无关 API 变化 | **排除**。本轮没有证明某个后续官方版本已解决全部边界，不能以“升级最新版”代替验证 |

接口依据：[PageIndex client][pi-client]、[parser protocol][pi-parser]、[策略选择][pi-pipeline]。分发依据：[pip 固定 VCS 提交](https://pip.pypa.io/en/stable/topics/vcs-support/)、[pip 哈希安装](https://pip.pypa.io/en/stable/topics/secure-installs/)。矩阵中的建议属于工程取舍，并非上游对 OpenKB 的兼容保证。

### 4.1 推荐补丁的最小范围

1. **请求执行接口**：让 sync/async 叶请求经过同一策略拥有者，或提供可注入的执行器；传入最终消息、模型、参数，返回结果或有类型的终止原因。关闭依赖内部自动异常重试后，OpenKB 才能明确拥有唯一自动尝试政策。认证、参数、确定超限、停止、预算终止直接向外传播；TOC 算法回退单独标注。
2. **内部循环边界**：覆盖 `check_toc`、`meta_processor` 三个同步分支和递归进入路径。可先等待线程化同步帮助函数，再评估原生异步转换；不在循环线程阻塞等待许可或结果。同步请求仍需有限等待和停止检查。
3. **统一生命周期入口**：优先允许调用者在一个明确拥有的异步生命周期里完成树、摘要、描述和日志收尾；保留同步入口时，用受控桥接且明确调用线程限制。若保留多循环，则必须在每个循环关闭前完成其日志和客户端收尾，不依赖最后新建循环统一补救。
4. **取消与质量契约**：gather 结果先传播停止／预算终止，再处理普通错误；异常清理覆盖停止路径。保留导航可降级，但空字符串或未展开节点必须返回可观察告警，不能自动证明内容和证据完整。

以上是建议补丁范围，不是已确定函数签名或已实现设计。它同时处理目前源码里互相牵连的阻塞、许可和重试，避免为每个调用点堆叠另一个不受预算管理的 wrapper。[管线][pi-pipeline]、[模型包装][pi-utils]、[目录算法][pi-tree]、[本地 add][pi-local]

### 4.2 可复现安装和许可证

保留原包时使用基线 `uv.lock`；启用补丁时必须同步更新依赖声明、来源与锁文件。不要覆盖上游同名同版本 wheel。可采用下游本地版本标识（例如 `0.3.0.dev3+openkb.1`，仅为命名候选），并记录原始 tag 对应提交、补丁提交、构建工具版本、产物 SHA-256、许可证和验收结果。运行环境从不可变 URL／受控 wheelhouse 取已验证产物；源码 fork 用完整 40 位 commit，不能只写研究 branch 或 PR ref。[pip VCS](https://pip.pypa.io/en/stable/topics/vcs-support/)、[版本规范：local version](https://packaging.python.org/en/latest/specifications/version-specifiers/#local-version-identifiers)

可供实施阶段生成的发布流程：在干净环境按固定源码及构建依赖构建 wheel；记录 hash；为目标平台解析完整依赖闭包；安装时使用完整带哈希 requirements 的 `pip install --require-hashes --only-binary=:all: -r ...`，或项目锁定工作流中等价的固定工件来源；验证实际安装元数据和关键源码。`--require-hashes` 要求全部依赖都有固定版本与哈希，不能只给 PageIndex 一行 hash 就宣称整个环境可复现。源码固定与 wheel 字节可重复构建也不是同一保证。本轮未执行这些安装／构建命令。[pip secure installs](https://pip.pypa.io/en/stable/topics/secure-installs/)

PageIndex 使用 MIT；本次涉及的 LiteLLM SDK 文件位于 `enterprise/` 外，对应 MIT，不能把整个仓库概括成全 MIT；OpenAI Python 使用 Apache-2.0。补丁／复制源码随分发保留相应许可证与版权声明；若修改并分发 OpenAI 源码，还应遵照其修改声明和适用 NOTICE 要求。此处只记录所读固定版本 LICENSE 的条款，未调查全部传递依赖许可证。[PageIndex LICENSE][pi-license]、[LiteLLM LICENSE][ll-license]、[OpenAI LICENSE][oa-license]

维护上将补丁数量、涉及内部文件、每次上游变更的冲突与测试成本记录下来。优先向上游贡献通用执行／取消修复，但上游合并不作为第一批唯一交付前提；上游新版本需重新审查与锁定，不能自动移除本地补丁。

## 5. 预算校准的观测方案

**不要先猜默认秒数。先保证每个边界有限、可观测，再用同一批代表资料与固定模型配置校准。** 本节是实施阶段方案，尚无本轮测量数据。沿用规格的共享文档处理用例作为主验收入口，内部仅替换真正的模型／网络 Adapter。[Spec: 大型 DOCX/PDF 可靠导入、PaddleOCR 与证据编译（三批设计）](https://github.com/wooveep/UrltraKB/issues/13)

| 边界 | 最小观测字段 | 可以回答／不能冒充什么 |
| --- | --- | --- |
| 任务／资料处理项 | 任务、资料版本、尝试、配置快照 ID；开始／停止请求／截止／终态的单调时钟 | 总耗时包括排队、计算、退避和收尾；不等于网络耗时 |
| 阶段与 PageIndex 子预算 | 解析、目录检测、TOC 模式／回退、验证、递归、摘要、描述、编译、提交；进入／退出／原因 | 区分导航和必要编译；避免导航把剩余编译预算用尽 |
| 每个逻辑模型调用 | span/parent ID、阶段、模型／provider、重试原因、幂等可知性、请求输入摘要、有效 timeout／输出上限 | 逻辑调用和算法新调用分开；不记录正文、凭证或完整请求 headers |
| 发出请求前 | 排队时间、许可数、预算预留、完整输入 token 估算与能力来源、预留输出 | 必须包含消息包装、系统提示、工具 schema、缓存块及实际多模态部分；原文字符数／节点 tokens 不足以证明请求适配 |
| SDK 调用与传输 | SDK 调用开始／结束、最终客户端 retry 值；可观测时记录 transport attempt、status/request ID | `completion/acompletion` 次数只能叫 SDK 可观测调用；内部 HTTP 次数不可见则明确 unknown |
| 响应／消耗 | 结果类别、finish_reason、服务端 usage、缓存计量、预留结算 | timeout 后 usage 缺失不等于零消耗；不把未知用量补零 |
| 循环与日志 | 独立 heartbeat 的计划／实际触发差、日志排队／开始／结束、超时与 flush/stop 结果 | 区分模型慢、事件循环不调度和日志自身慢；日志故障不覆盖业务终态 |
| 停止与退出 | 停止看到时间、后续请求是否被拦截、在途取消完成、结果作废、客户端关闭、writer/worker 退出、恢复完成、锁释放 | “已要求停止”“已停止等待”“已安全结束”是不同事实 |

数据来源与可见性由当前 [PageIndex leaf][pi-utils]、[LiteLLM LoggingWorker][ll-logging]、[SDK 客户端][ll-openai] 决定。未提交 `WorkerDiagnostics` 当前包装 `completion/acompletion` 的计数只能作为上表中的 SDK 调用起点；不能给它改个名字就声称是 HTTP 请求计数。请求内容只在本地执行中计算预算，公开诊断保存长度、摘要、类别和计量，不保存凭证或文档全文。

执行预算的建议规则：

- 为每次请求计算 `有效时限 = min(已解析的请求时限, 阶段剩余, 资料处理项剩余)`；全部值先检查有限且为正。请求超时、阶段预算、处理项预算与辅助收尾限额分别配置和记录，不能互相替代。
- 取得许可、预留此次尝试的额度后再发请求；保证 `已消耗 + 在途预留` 不超过阶段／处理项限制。退避也受剩余时间约束，每次重试重新核算，停止或预算终止不进入退避。
- 上下文容量取可核对的模型元数据或显式配置，未知自定义模型停在配置验证，不用经验猜值。完整输入加预留输出必须满足容量；`drop_params` 不能使关键输出限制被静默丢弃后仍宣称预算生效。
- 样本固定为有／无／错目录 PDF、大节点递归 PDF、较大 DOCX 与固定解析产物；覆盖正常、慢响应、401／参数错、429／暂时服务错、重试退避、取消和日志慢回调。先使用离线受控 Adapter，再在另行授权的真实运行中采集分布；按资料规模、目标模型／provider、缓存与并发分层后校准默认值，不能只报一个平均值。
- 对照验收同时记录内容与事实覆盖、有效来源定位、最大完整输入、峰值资源、累计 usage／unknown、导航降级及人工检查量。速度改善不能替代完整性门槛。

## 6. 下一张决策票只需要解决的最小未知

1. **PageIndex 补丁入口与维护归属。** 选择可注入执行器＋受控异步入口的最小改动，还是先采用固定版本内部适配作为过渡；用现有真实 `LocalBackend → build_index → tree_parser/meta_processor` 控制流的离线探针证明覆盖，不能只测新造的小 wrapper。明确补丁仓库、工件发布位置及负责人；产品范围无需重问。
2. **目标 provider 的“零隐式重试”和资源所有权。** 固定第一批实际使用的 provider 路径，捕获构造参数、SDK／transport 尝试与 owned client。验证 `num_retries=0,max_retries=0` 是否抵达该分支，缓存命中是否保留正确配置，sync/async 正常／失败／停止是否都关闭。只有证明现有接口不足才决定是否补丁 LiteLLM。
3. **取消落点与终止恢复。** 以慢模型、等待许可、TOC 回退、gather 子任务和索引 blob 复制后停止为故障点，确认停止类型不被吞掉、不发新尝试、没有迟到写入；有写能力的执行体退出后才能确认恢复。实际 worker 退出和 Windows SQLite 释放仍是验收项。
4. **预算默认值的证据。** 确定代表语料、模型能力来源与要观测的计量口径，待另行授权的真实测量给出请求／阶段／处理项／收尾限额。当前未知的是数值和 provider 行为，不是是否要有限预算。

未证明事项明确保留：用户原 PDF 的具体超时现场；真实 HTTP 尝试与未知计费消耗；跨循环客户端是否在目标 provider 上产生实际故障；socket 泄漏是否存在；补丁后的性能及 Windows/Linux 运行表现。**本轮没有证据把其中任何一项标为已修复。**

## 来源索引

以下 PageIndex／LiteLLM／OpenAI 源码链接固定到上文提交；OpenKB 链接固定到研究基线。官方文档为 2026-09-09 读取版本，文档更新不自动改变锁定代码结论。

[kb-deps]: https://github.com/wooveep/UrltraKB/blob/b5cdc0d4574bf4b402b71b367fad22794bc2604e/pyproject.toml
[kb-lock]: https://github.com/wooveep/UrltraKB/blob/b5cdc0d4574bf4b402b71b367fad22794bc2604e/uv.lock
[kb-indexer]: https://github.com/wooveep/UrltraKB/blob/b5cdc0d4574bf4b402b71b367fad22794bc2604e/openkb/indexer.py#L156
[kb-compiler]: https://github.com/wooveep/UrltraKB/blob/b5cdc0d4574bf4b402b71b367fad22794bc2604e/openkb/agent/compiler.py#L1988
[kb-documents]: https://github.com/wooveep/UrltraKB/blob/b5cdc0d4574bf4b402b71b367fad22794bc2604e/openkb/application/documents.py#L77
[pi-client]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pageindex/client.py
[pi-collection]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pageindex/collection.py#L53
[pi-local]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pageindex/backend/local.py#L83
[pi-config]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pageindex/config.py
[pi-pipeline]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pageindex/index/pipeline.py#L49
[pi-tree]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pageindex/index/page_index.py#L969
[pi-utils]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pageindex/index/utils.py#L115
[pi-pdf]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pageindex/parser/pdf.py
[pi-parser]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pageindex/parser/protocol.py
[pi-storage]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/pageindex/storage/sqlite.py#L214
[ll-utils]: https://github.com/BerriAI/litellm/blob/1296275dc52d9f4e05696380735037fbb841fcc3/litellm/utils.py#L1213
[ll-main]: https://github.com/BerriAI/litellm/blob/1296275dc52d9f4e05696380735037fbb841fcc3/litellm/main.py#L1380
[ll-openai]: https://github.com/BerriAI/litellm/blob/1296275dc52d9f4e05696380735037fbb841fcc3/litellm/llms/openai/openai.py#L354
[ll-logging]: https://github.com/BerriAI/litellm/blob/1296275dc52d9f4e05696380735037fbb841fcc3/litellm/litellm_core_utils/logging_worker.py
[ll-cleanup]: https://github.com/BerriAI/litellm/blob/1296275dc52d9f4e05696380735037fbb841fcc3/litellm/llms/custom_httpx/async_client_cleanup.py
[oa-client]: https://github.com/openai/openai-python/blob/6d9262d5c666a1e4d47f63178db907ba3087ac5d/src/openai/_base_client.py
[oa-constants]: https://github.com/openai/openai-python/blob/6d9262d5c666a1e4d47f63178db907ba3087ac5d/src/openai/_constants.py
[pi-license]: https://github.com/VectifyAI/PageIndex/blob/9ad54122bbd519cec8913198e2d63cff92781c1e/LICENSE
[ll-license]: https://github.com/BerriAI/litellm/blob/1296275dc52d9f4e05696380735037fbb841fcc3/LICENSE
[oa-license]: https://github.com/openai/openai-python/blob/6d9262d5c666a1e4d47f63178db907ba3087ac5d/LICENSE
