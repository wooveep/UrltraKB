# Markdown 内容块与原文读取

Markdown 使用冻结正文的固定 `cl100k_base` token 数分类：≤5000 为短文。
短文的首个完整请求容量不足时也会分段；这不会把它改成“长文”。
分段入口使用原 PageIndex 内容算法，节点摘要来自原文，概念编译仍使用完整树。

规范化时按 Markdown 自然结构组织目标约 2000 Unicode 码点的块。超大段落、
代码或表格在字素边界细分，当前策略不重叠，空白和 CRLF 原样覆盖。重复标题
路径、表头和补围栏属于 `display_context`，单独记录其原文范围或补充类型；
它们计入索引请求预算，不重复计入文档长度，也不是可引用的原文片段。

`sources/<name>.content.json` 保存正文、字符映射、块列表、资产摘要和策略。
同阶段产生的 `.okbi` 包包含这些数据及资产字节。索引解析器只读这个包，不
再次切块或摘要。受管包、缓存身份、完整树和范围恢复都保留同一块策略。
缓存缺失后，从保留包重建不读取原位置文件，不调用模型。

没有原文标题的段落允许生成标签，树节点记录 `title_origin=generated`，并
携带经验证的块号、正文部位、Unicode 范围及原文摘录。原文标题记录
`title_origin=original`。导航条目不能代替原文标题锚点。块不使用印刷页码
偏移或 PDF 的“末标题须过文档中点”规则；PDF 的既有行为与递归 AND 条件保留。

CLI 使用 `openkb source SOURCE_ID --blocks 1,3-5`，也可用 `--chars START:END`。
REST `/api/v1/document/source` 使用 `blocks` 字段；`pages`、`chars`、`blocks`
只能选一种。块结果显示 `pages=null`、`block_count`、`block_range`、精确
`source_spans` 与独立显示上下文。桌面支持块范围与字符范围，问答通过受版本
视图约束的 `get_block_content` 读取。重编译复用原有块、决策和索引。

本地 PageIndex 补丁版本为 `0.3.0.dev3+urltrakb.3`，未更换上游基线。
字素分割显式固定 `regex==2026.5.9`（原锁文件及 tiktoken 依赖已使用此版本；
许可证为 Apache-2.0 AND CNRI-Python），沿用已固定的 `markdown-it-py==4.2.0`。
这些策略及固定计量器、正文、资产和执行配置共同参与缓存身份。

自动验证使用真实导入、发布、读回、重编译和本地缓存，只替换外部模型。
它不替代真实模型质量、Windows 或 Debian 桌面发行的人工验收。
