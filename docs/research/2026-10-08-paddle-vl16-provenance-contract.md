# 飞桨 VL-1.6 jobs：原页、区域与图像出处的契约增量研究

日期：2026-10-08。OpenKB 基线：`be5aad49cffac16d36e4c676f099467a5ab3cc22`。
研究票：[研究：飞桨 jobs 的版面结果能否支持首版 PDF 的原页与区域出处？][ticket]。
所属地图：[PDF 识别决策地图：云端 OCR、图内描述与可恢复导入][map]。

状态：公开资料与固定源码调查完成；7 项隔离函数检查通过；目标服务、图像质量与恢复能力尚未实测。
本轮没有调用 OCR、上传文件、读取 OCR 凭证、安装模型／依赖或修改业务代码。
网络获取使用 agent-reach 的 GitHub／`gh` 与网页／Jina Reader 路由，另用 web 工具交叉阅读官方页面。

## 结论

首版能够保留可靠的**原页级出处**，前提仍是已接受的实际预切片、独立输出绑定与逐页验证。
本轮没有发现足以把 jobs 输出下标当成原页身份的新公开保证；单页输入也要检查零／多结果、内容和必需资产。
沿用 [决策：云 OCR 如何确定分片、原页映射与继续处理方案？][prior-decision]，`pageRanges` 继续后置。

区域与图片能力需要独立记录：

- jobs 官方示例与 SDK 支持 JSONL → `result.layoutParsingResults` → Markdown／图片资源；**没有完整的、版本化的 jobs 页级 JSON Schema**。[jobs 指南][jobs]、[SDK parser][sdk-parse]
- `prunedResult` 删除 `page_index` 是**同步服务及其源码**的事实；不能扩写为 jobs 所有层都必无页标识，也不能反向假设同步其余字段全被 jobs 保留。[同步接口][sync-doc]、[递归裁剪函数][px-prune]
- `block_bbox`／多边形在预处理后的页面像素空间；方向矫正、去畸变、PDF 渲染及返回图缩放需要分别处理。现有序列化没有提供完整的原 PDF 逆变换链。[产线输入与输出][px-pipeline-space]、[预处理 JSON][px-preprocess-json]
- **Markdown 图片不天然满足“仅图内内容”**：固定源码存在矩形额外裁剪覆盖同路径多边形遮罩裁剪的路径。实际送入 DeepSeek 的图像字节必须检查宿主正文泄漏。[区域裁剪][px-crop]、[额外图片采集][px-gather]、[Markdown 合并][px-markdown]
- `done`、页绑定、资产可读和内容可用是四种不同证据；failed job 没有已公开的部分页结果契约。[jobs 状态][jobs-query]、[SDK 结果对象][sdk-results]
- 用户的**完整 jobs 地址**不是官方 SDK 的 `base_url` 参数；后者会再追加固定路径。恢复还需保留原部署与 job 的关联，Key 轮换不等于证明新 Key 能读取旧 job。[SDK HTTP][sdk-http]

## 来源范围与等级

| 等级 | 本文含义 | 固定范围及限制 |
| --- | --- | --- |
| D | 文档明示 | jobs 专用指南、错误表，核对日为本文日期；网页没有不可变版本号。 |
| S | 源码行为 | 下列固定提交的代码；不是线上部署保证，也不等于真实服务已通过。 |
| A | 相邻接口证据 | 同步 `/layout-parsing`、本地完整产线和服务化代码；只能解释候选结构。 |
| I | 工程推断 | 从来源推导的结果契约约束或待讨论方案，明确不当供应商承诺。 |
| U | 未确认 | 所查公开资料没有充分证明，保留为验证门槛。 |

PaddleOCR SDK 固定到发行 `v3.7.0` 的提交 `b03f46425e8ff4442b268ce449e3eef758146cd4`。
与 [先前研究][prior-report] 使用的 `2661c7c0ef5c613e8f93c6e93b2e052399f0f854` 比较，`paddleocr/_api_client/` 没有文件差异；此前的 parser／恢复限制仍成立。[SDK 比较][sdk-compare]

PaddleX 固定到 `release/3.7` 的 `ffb64904d23708863ff5b8da312a5cbd52a7f462`。
其相对 `v3.7.0`（`e0068ce0bfe75b2992e5b38d06a0393c70f887f7`）的 3 个提交未改动本文读取的产线、服务化、结果及裁剪文件。[PaddleX 比较][px-compare]
AI Studio 的 VL-1.6 服务介绍声明 PaddleX 3.7.0；**没有证明托管 jobs 正运行上述任一 commit**。[托管 VL 服务介绍][hosted-vl]

## 相对已有调查的证据差异

| 问题 | 本轮可证明的增量 | 等级与不能推出的结论 |
| --- | --- | --- |
| VL-1.6 可用性 | SDK 模型枚举包括 `PaddleOCR-VL-1.6`；托管服务介绍有 1.6；jobs 专用模型表仍列到 1.5。[模型枚举][sdk-models]、[托管说明][hosted-vl]、[jobs 参数][jobs-submit] | D/S：名称有一方依据；U：当前账号、完整端点、选定选项的实际可用性。 |
| 模型身份回显 | jobs 提交／查询表不含模型构建版本或输入摘要回显；SDK `Job.model` 是客户端对象字段，`JobStatus` 没有模型字段。[查询表][jobs-query]、[结果类][sdk-results] | U：不能拿请求中的模型名当服务真实模型版本证明。记录请求值与实际可得回执，未知版本保持未知。 |
| JSONL 容器 | SDK 按行取 `result`，再展开每行 `layoutParsingResults`；`dataInfo` 后行同名键覆盖前行。[parser][sdk-parse] | S：一行不保证一页；展开结果丢失行／项定位，合并后的 `dataInfo` 不足以审计原始覆盖。 |
| 页项结构 | SDK 要求 `markdown.text` 的键存在，却不验证其类型；`prunedResult` 可缺失，图片字段可缺失或为 null。[parser][sdk-parse] | S：成功解析不证明结构化版面、文字或图片完整；需在应用边界独立验证。 |
| 同步版面结构 | 序列化有尺寸、设置、块列表、bbox、块编号、顺序及可选多边形；普通 JSON 默认不内嵌块的 image 对象。[结果序列化][px-result-json] | A/S：候选可消费字段；U：jobs/VL-1.6 是否保留、类型与选项效果。 |
| 同步身份裁剪 | `prune_result` 递归删除 `input_path` 与 `page_index`，不仅删除顶层。[裁剪函数][px-prune] | A/S：不能从重构结果中的同名嵌套字段补回已删除身份。jobs 示例的 `page_num` 是本地计数器。 |
| 图像控制 | `returnMarkdownImages=false` 时同步返回 null；`visualize` 控制 `inputImage` 和中间图。图片后处理可编码或返回 URL，并可缩放。[服务化][px-service]、[图片后处理][px-images] | A/S：缺图可能是配置结果；U：jobs 的返回形式与服务端缩放配置。不得把缺图自动判成文档无图。 |
| 跨页重构 | 同步接口可在返回前调用 `restructure_pages`，合并表格／调整标题；默认不重构。[服务化调用][px-service]、[请求 schema][px-schema] | A/S：页项数量不变也不证明正文仍限于本页。是否使用及如何保存跨页关系须进入处理身份。 |

## 原页绑定：三种位置分别保存

1. **原文物理页**：属于不可变资料版本，不等于页码印刷文字。
2. **实际子文件物理页**：本地切片回读产生输入映射，连同子文件摘要保存。
3. **响应位置**：至少保留 job、JSONL 行、页项位置与可得服务标识；它不是自动可信的原页号。

以上沿用既有决定；前两者的映射不能证明第三者。官方 jobs 示例按行／数组递增 `page_num` 只用于输出文件命名；SDK 也只是按遍历顺序追加，没有身份或重复检查。[已有决定][prior-decision]、[jobs 示例][jobs]、[parser][sdk-parse]

多页结果需要目标部署提供能持续核验的身份依据，再逐次验证唯一性和预期覆盖。
尺寸、图像相似度或识别出的页码只可作为佐证；重复／相似页使这些信号可能不唯一。
“输入 3 页、返回 3 项”仍容许错序或漏一页而重复另一页，不能消除歧义。[已有页绑定准入决定][prior-decision]

缺该依据时沿用**提交前规划单页子文件**的已确认路线。
单页 job 的正确关联仅解决输入集合唯一性；空数组、多项结果、正文不足、必需图片缺失仍须报告未完成／待检查。
不把单页路径描述为免验收，也不在本轮引入自动重提未知多页任务的方案。[已有决定][prior-decision]

同步 `dataInfo.pages` 的元素只有像素宽高；`numPages` 由实际渲染结果列表长度产生。
它不是不可变原文件总页数或原页 ID。SDK 又会跨 JSONL 行覆盖 `dataInfo`，因此不能单凭该对象证明全文覆盖。[同步数据类型][px-data-info]、[PDF 读取][px-read-pdf]、[SDK parser][sdk-parse]

## 几何链与精确高亮的边界

固定产线的链条为：

```text
原 PDF 页 → 服务端渲染页图 → 方向矫正 → 可选去畸变
         → 在处理后页图检测／裁剪 → 序列化块坐标
         → 可选缩放／编码返回页图和裁剪图
```

这是源码链路，不是 jobs 对每一步的回显保证。[PDF 读取][px-read-pdf]、[预处理][px-preprocess]、[版面输入][px-pipeline-space]、[图片后处理][px-images]

| 域／变换 | 证据 | 对可回读结果的含义（I） |
| --- | --- | --- |
| PDF → 渲染像素 | 同步代码用 PDF 渲染器，记录渲染后宽高；没有在 `PDFPageInfo` 中导出 PDF 点坐标变换。[PDF 读取][px-read-pdf]、[数据类型][px-data-info] | 记录原 PDF 物理页与本地页面属性；不能把服务 bbox 数值直接写成 PDF 点坐标。 |
| 方向矫正 | 预处理结果含 `angle`，处理后图用于下一阶段；未启用时角度为 -1。[预处理][px-preprocess] | 只有确认角度语义、源尺寸与裁剪／渲染关系，才能组成可逆旋转链；`angle` 一个值不代表全链已知。 |
| 去畸变 | 产线消费矫正后的图；JSON 只保存设置和角度，没有稠密逆映射。[预处理][px-preprocess]、[预处理 JSON][px-preprocess-json] | 非线性变形不能靠宽高比例还原；该证据不足以保证在原 PDF 上精确高亮。 |
| 版面框／多边形 | `width/height` 来自预处理输出；裁剪在该图像上按左、上、右、下坐标切片；可按多边形涂白。[产线尺寸][px-pipeline-space]、[裁剪][px-crop] | 保存处理后像素域、尺寸、bbox／polygon；域映射未知时保持未知。 |
| 返回资产尺寸 | 服务化可对 `inputImage`、可视化图和 Markdown 图片应用 `max_output_img_size`。[服务化][px-service]、[后处理][px-images] | 实际图片尺寸未必等于坐标域尺寸；下载后测量并保存，不能直接把框绘制到缩放图上。 |
| 块身份／顺序 | `block_id` 是列表枚举，`block_order` 对图片、表格等标签可能为 null；有条件附加全局编号。[序列化][px-result-json] | 块身份依附处理版本和已绑定页；不能作为跨版本永久引用键。 |

**可支持的分级表达（I，待结果契约决定）**：保留“原页已绑定”“处理后区域已定位”“映射回原 PDF 已验证”三个独立事实。
页身份成立而几何映射未知时，现有证据支持回到原页阅读、展示受管裁剪图；不支持声称原 PDF 精确区域已验证。
是否因此限制某项交互、如何显示精度，归 [决策：识别正文与图内描述如何冻结为统一、可回读的处理结果？][result-decision]。

## 图片资产与图内描述隔离

固定源码有两条裁剪路径：`CropByBoxes` 可将多边形外像素涂白，块装配把它放入 `block.image`；另一路 `gather_imgs` 对图片类别做矩形裁剪。
Markdown 转换先收集 `block.image`，最后把 `imgs_in_doc` 以同路径覆盖进去。
所以在路径相同的情况下，返回图片可能成为矩形裁剪，即使块带多边形。第 7 项隔离检查复现了这一覆盖行为。[裁剪][px-crop]、[块图片装配][px-assemble]、[采集][px-gather]、[合并][px-markdown]

由此不能证明每张返回图都包含宿主正文，但也不能证明已把它排除。
检测框可能跨越相邻文字；异形图的外接矩形可包含图外区域；`inputImage` 和带标注的 `outputImages` 更不是合格的纯图内描述输入。
这些是从裁剪路径与已确认描述边界推导的风险，须验证实际像素，不能仅检查字段名。[裁剪源码][px-gather]、[服务化图像集合][px-service]、[产品前提][map]

结果契约需要表达：原图／裁剪的实际字节摘要、像素尺寸、所属原页、已知区域、裁剪／遮罩策略、几何可信级别，以及本次送入描述的图内转写身份。
供应商相对图片路径只是一次结果的键：当前构造方式由标签和整数框坐标组成，没有资料／job／页身份；跨页同框同名可能碰撞。[图片路径生成][px-gather]

因此受管资产和图片出现位置分别保存，重命名或去重时更新 Markdown 引用；共享描述缓存按真正传入的图像与图内转写计算。
图注／附近正文继续只供后续编译和问答，不能用整页缩略图补充进描述输入。[已确认产品前提][map]

## `done`、页成功、资产完整与内容可用

| 观察／校验 | 能证明什么 | 仍不能证明什么 |
| --- | --- | --- |
| 远端 `done` | 可以取回结果定位。[jobs 查询][jobs-query] | JSONL 完整、页绑定正确、图片已落盘或内容可靠。 |
| JSONL／SDK 解析成功 | 返回形状足以让 parser 构造对象。[parser][sdk-parse] | 非空、去重、页覆盖、`prunedResult` 存在或文字类型正确；本轮隔离检查已确认这些边界。 |
| 原页绑定通过 | 本次响应对应已知输入页，预期集合没有无法解释的缺／重／额外项。[既有决定][prior-decision] | 文字完整、原 PDF 区域映射、图内信息或资产质量。 |
| 必需资产落盘并校验 | 描述／阅读所需资产可用、引用闭合；不会依赖短链长期存活。[既有资产决定][prior-decision] | 识别文字语义正确、图片区域隔离已通过。 |
| 内容可用 | 应由已确认的原生／OCR／图内描述规则和验收判定。[地图][map] | 空白与漏识别不能靠空字符串区分；版面检测分数不等于转写正确率。 |
| job `failed` 且进度大于零 | 只证明报告过进度；公开表明确没有部分页成功情况。[jobs 查询][jobs-query] | 不能据计数创建成功页检查点，亦无可单独验证的部分页结果下载保证。 |

对 300 页成功 297 页、失败 3 页的已确认场景，297 页必须来自已经验证并持久化的结果，不能从某个 failed job 的进度计数推出来。
如果 300 页在同一失败 job 内，公开证据不足以声称服务可取回前 297 页；这是分片粒度与恢复验收必须面对的事实。
用户的全扫描暂停优先级及非全扫描局部失败继续规则保持不变。[jobs 状态][jobs-query]、[地图产品规则][map]

“必需资产”应按消费用途判定：缺某张诊断可视化图，与缺描述实际依赖的原图不是同一失败。
OCR 正文成功而描述资产失败，须能分别记录，不把描述失败冒充必要正文 OCR 失败；具体判定归 [决策：如何判定全扫描正文、必要 OCR 与可用识别内容？][routing-decision]。

## 自定义 jobs 地址、Key 与恢复关联

| 主题 | 已核实事实 | 结果／恢复契约需要保留的内容（I） |
| --- | --- | --- |
| 完整端点 | SDK `base_url.rstrip('/') + '/api/v2/ocr/jobs'`；查询再追加 `/{jobId}`。[HTTP 构造][sdk-http] | 用户配置的是完整 jobs URL。适配层必须明确转换或直接请求；不得双追加路径，也不能默默丢掉自定义前缀。 |
| job 关联 | 公开 GET 以该部署的 jobId 和 Bearer Token 查询；SDK Job 仅保存 jobId、请求模型与任务种类。[jobs 查询][jobs-query]、[SDK Job][sdk-results] | `(原完整端点, jobId, 本地尝试, 不可变输入/选项)`；不能把新端点与旧 jobId 任意组合。 |
| API Key | jobs POST／GET 均要求 Bearer；未找到跨账号读取、同账号轮换或旧 Key 撤销后的 job 访问保证。[jobs 协议][jobs-submit] | 只保留不含秘密的凭据配置来源／关联说明；恢复时取当前可用凭据，不把 Key 值写入内容身份。是否可读须由服务响应确认。 |
| 结果短链 | SDK 对 JSONL 与图片使用独立 `requests.get`，没有附带 jobs Bearer；官方称结果为 BOS 短链。[HTTP 下载][sdk-http]、[资源下载][sdk-resources] | 受管本地资产独立于 Key；远端定位受控暂存。轮换 Key 既不证明旧短链失效，也不证明其仍有效。 |
| 模型与部署能力 | 所查查询 schema 没有服务构建号；同步代码版本也不能证明托管实现。[查询表][jobs-query]、[托管说明][hosted-vl] | 保存能力证据的端点、请求模型／选项、采样时间、原始响应摘要和验证方法；部署或影响结果的策略变化时重评。 |
| 幂等、取消、过期 | 已有公开指南列过期／未找到错误，但没有固定保留时长、幂等键或取消接口保证。[jobs 错误][jobs-errors] | 延续未知提交不盲重交、已知任务先查询、本地停止不冒称云取消；不假设查询必刷新短链。 |

Key 轮换不改变已保存内容的缓存身份，是用户已确认的产品规则；它与“新凭据能否访问旧远端任务”是不同命题。[地图][map]
恢复时若原端点凭据已清除或无法访问，不能因此把已知 job 改为未提交，也不能自动上传至新的地址。
如何让用户补回可用连接及决定新的尝试，归 [决策：识别暂停与补识别如何跨任务恢复并使用 OCR 配置？][recovery-decision]。

## 可交给统一结果契约的最小事实

以下是候选字段职责，不指定存储格式，不替 HITL 选定降级行为：

1. 不可变原件／切片摘要、原物理页集合、切片输入映射；页绑定验证另存。
2. 原完整 jobs 地址、请求模型／选项、适配器版本、尝试身份、jobId、最后远端观测；凭据值排除在外。
3. 原始 JSONL 的行／项定位和受控证据摘要；缺失／未知字段不能靠默认值伪造。
4. 页的绑定状态、正文 OCR 状态、图内描述状态、真正空白／待检查、内容可用性分别表示。
5. 区域的处理后像素域、宽高、bbox／polygon、可得变换链；原 PDF 映射未验证时明确为空／未知。
6. 资产的实际摘要／尺寸、受管位置、相对引用改写、必要用途；图像出现位置独立于共享描述缓存。
7. 完成检查点由页、内容及必需资产验证产生；远端 done／进度不能直接替代。

职责依据为上文已核验源码及 [既有分片、来源、检查点决定][prior-decision]、[本次产品前提][map]；字段命名不是供应商 schema。

## 最小后续合成探针：本轮均未执行真实服务调用

先生成不含用户资料的 6 页 PDF：唯一文字与图案页、空白页、两张相似页、旋转页、异形图旁紧邻正文页。
保留所有页的原始字节、渲染和预期区域；另造一张曲面／弯曲合成图测试去畸变。
探针按已经选定的实际子文件路径发送；不通过本票启用 `pageRanges`。

| 探针 | 最小待证命题 | 判断出口 |
| --- | --- | --- |
| 模型与页身份 | 同一配置下单页子文件和一个多页子文件；记录原始外层／每行／页项标识与可得模型回执。 | 证明目标端点接受 VL-1.6 和选项；无持续可核验身份时多页仍不准入。有限顺序一致不算永久保证。 |
| 坐标域 | 同一旋转页分别关闭／开启方向矫正，另测去畸变；核对输入图、处理图、bbox 与实际图片尺寸。 | 分清像素域和可逆关系；无逆映射只得到页级／处理后图像证据，不能声称原 PDF 精确高亮通过。 |
| 图内隔离 | 异形图外接矩形内放“宿主专用文本”，图内放另一标识；核对下载图、遮罩与最终送入描述的字节。 | 图外文字不得出现在描述输入；不能仅以 `block_polygon_points` 存在判通过。 |
| 图片返回选项 | 小样本对照 `returnMarkdownImages`、`visualize`；核对 URL／编码、null／缺失、图像尺寸与 Markdown 引用。 | 记录目标部署实际能力；没有图片与没请求图片分别处理。 |
| 空白／相似／缺重页 | 服务小样本观察空白占位；离线适配器注入缺项、重复、同长错序、零项与多项单页响应。 | 没有可靠绑定或无法解释空白时保持待检查；SDK 能解析不算成功。 |
| failed／done／资产失败 | 离线模拟一个 failed 多页 job、一个独立成功片、done 但 JSONL 截断／必需图下载失败。 | 保留真正有效片；不从进度恢复页；正文和描述失败分开；全扫描／混合 PDF 按既定规则验收。 |
| 完整 URL／Key 轮换 | 离线记录实际请求 URL；授权后以同账户轮换 Key 查询原 job，另测清除覆盖和新端点配置。 | 验证无双追加路径，旧任务仍查原部署；账户关联无法确认则标证据不足。不能用跨账户成功假设作前提。 |
| 短链／期限 | 授权后对少量原 job 重查并比较旧／新资源定位；不启动持续监控。 | 只记录观察到的可用下界；固定保留期、链接刷新与远端取消仍需明确服务契约。 |

质量、预算和停止门槛归 [决策：首版 PDF 识别以哪些质量、恢复与配置证据移交实施？][acceptance-decision]；本报告不把服务硬上限转为产品默认值。

## 本轮验证与待决问题

已读取既有研究和决策、官方 jobs 页面、VL-1.6 服务介绍、发行 SDK 及 PaddleX 固定源码，并对上述版本差异核验。
临时源码和探针只放 `/tmp`；研究分支只提交本文。

离线检查从固定源码 AST 仅抽取函数，使用标准库 dataclass／合成对象运行；没有导入 PaddleOCR／PaddleX 包、启动模型或发出网络请求。
7 项结果如下；这些是消费边界的复核，不是服务测试，也不是 OpenKB 集成测试。

| 输入／检查 | 固定源码观察 |
| --- | --- |
| parser 接收空 JSONL 列表 | 返回 0 页，无异常。 |
| 页仅有 `markdown.text` | 接受，`pruned_result=None`。 |
| 同一页对象重复两次 | 返回 2 页，不检查重复。 |
| `text`、`images`、`outputImages` 为 null | 接受，保留 null。 |
| 两行的 `dataInfo.pages/numPages` 不同 | 后行覆盖前行同名值。 |
| prune 输入含顶层及嵌套 `page_index` | 两层均删除。 |
| 块图片与 `imgs_in_doc` 使用同一路径、不同哨兵值 | 最终采用额外图片值，覆盖块图片值。 |

前 5 项依据 [SDK parser][sdk-parse]，后 2 项依据 [prune][px-prune] 与 [MarkdownConverter][px-markdown]。
文档另检查 Markdown 引用定义、围栏、固定提交 URL、相对版本对照和 Git whitespace；没有运行业务测试。
Agent Reach 检查结果为 v1.5.0，当前已是最新；未安装或更新工具。

本轮发现的具体问题都能纳入已有决策，无需单独新增票：

- [决策：识别正文与图内描述如何冻结为统一、可回读的处理结果？][result-decision]：是否采用独立的页／处理后区域／原 PDF 区域精度表示；用什么可检查的裁剪或遮罩结果满足图内边界；必需资产按何种用途记录。
- [决策：识别暂停与补识别如何跨任务恢复并使用 OCR 配置？][recovery-decision]：原完整端点仍在、旧凭据已不可用时的恢复交互，以及完整 URL 与 SDK 固定路径的适配契约。
- [决策：首版 PDF 识别以哪些质量、恢复与配置证据移交实施？][acceptance-decision]：目标 jobs 的字段保留、单页内容／资产完整、几何与图内隔离探针需哪些证据才算通过。

这些问题不改变用户已确认的全扫描暂停、混合 PDF 局部失败继续、图内描述范围、用户自行配置 API／Key 或旧引用继续可读的要求。[地图][map]

[ticket]: https://github.com/wooveep/UrltraKB/issues/127
[map]: https://github.com/wooveep/UrltraKB/issues/126
[prior-decision]: https://github.com/wooveep/UrltraKB/issues/20#issuecomment-5601281768
[prior-report]: https://github.com/wooveep/UrltraKB/blob/80f31859214bbffd1529084f0a3950200d6e747e/docs/internal/research/2026-09-09-ingestion-paddleocr-cloud-contract.md
[routing-decision]: https://github.com/wooveep/UrltraKB/issues/129
[result-decision]: https://github.com/wooveep/UrltraKB/issues/130
[recovery-decision]: https://github.com/wooveep/UrltraKB/issues/131
[acceptance-decision]: https://github.com/wooveep/UrltraKB/issues/132
[jobs]: https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5
[jobs-submit]: https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5#%E6%8F%90%E4%BA%A4%E8%A7%A3%E6%9E%90%E4%BB%BB%E5%8A%A1
[jobs-query]: https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5#%E8%8E%B7%E5%8F%96%E8%A7%A3%E6%9E%90%E7%BB%93%E6%9E%9C
[jobs-errors]: https://ai.baidu.com/ai-doc/AISTUDIO/fml7mozw5#%E9%94%99%E8%AF%AF%E7%A0%81
[hosted-vl]: https://ai.baidu.com/ai-doc/AISTUDIO/Cmkz2m0ma
[sync-doc]: https://www.paddleocr.ai/latest/version3.x/pipeline_usage/PaddleOCR-VL.html#43
[sdk-compare]: https://github.com/PaddlePaddle/PaddleOCR/compare/b03f46425e8ff4442b268ce449e3eef758146cd4...2661c7c0ef5c613e8f93c6e93b2e052399f0f854
[sdk-models]: https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/paddleocr/_api_client/models.py#L23-L47
[sdk-results]: https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/paddleocr/_api_client/results.py#L28-L75
[sdk-parse]: https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/paddleocr/_api_client/_poller.py#L124-L152
[sdk-http]: https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/paddleocr/_api_client/_http.py#L75-L203
[sdk-resources]: https://github.com/PaddlePaddle/PaddleOCR/blob/b03f46425e8ff4442b268ce449e3eef758146cd4/paddleocr/_api_client/_resources.py#L27-L119
[px-compare]: https://github.com/PaddlePaddle/PaddleX/compare/e0068ce0bfe75b2992e5b38d06a0393c70f887f7...ffb64904d23708863ff5b8da312a5cbd52a7f462
[px-result-json]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/paddleocr_vl/result.py#L358-L460
[px-prune]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/serving/basic_serving/_pipeline_apps/_common/common.py#L29-L42
[px-images]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/serving/basic_serving/_pipeline_apps/_common/common.py#L46-L107
[px-service]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/serving/basic_serving/_pipeline_apps/paddleocr_vl.py#L60-L172
[px-schema]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/serving/schemas/paddleocr_vl.py#L37-L71
[px-pipeline-space]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/paddleocr_vl/pipeline.py#L870-L1016
[px-preprocess]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/doc_preprocessor/pipeline.py#L170-L208
[px-preprocess-json]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/doc_preprocessor/result.py#L82-L98
[px-crop]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/components/common/crop_image_regions.py#L40-L80
[px-gather]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/layout_parsing/utils.py#L333-L382
[px-assemble]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/pipelines/paddleocr_vl/pipeline.py#L605-L633
[px-markdown]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/common/result/converter/markdown_converter.py#L65-L136
[px-data-info]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/serving/infra/models.py#L60-L83
[px-read-pdf]: https://github.com/PaddlePaddle/PaddleX/blob/ffb64904d23708863ff5b8da312a5cbd52a7f462/paddlex/inference/serving/infra/utils.py#L276-L320
