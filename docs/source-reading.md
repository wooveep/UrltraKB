# 阅读冻结来源与 PDF 物理页

导入后的来源阅读使用成功发布修订保留的正文、页映射和图片。原始用户文件
移动或删除不会改变这些资料。新 PDF 保留从 1 开始的连续物理页序，包含空白页；
印刷页码（例如 i、ii、iii）独立显示，不用于范围定位。未提取到文字或图片的页
会明确标注，不能据此断定 PDF 的可视页面为空。

短 PDF 仍按原有 Markdown 正文编译，页映射仅服务于来源阅读；长 PDF 继续使用
原有 PageIndex 内容索引。PDF 的两处提取实现没有合并。已发布的处理指纹、索引、
原件和资产随修订保留，重编译与知识刷新复用对应输入。

在桌面的资料列表中选择“阅读原文”，输入 `2` 或 `1,3-5` 读取物理页，留空读取
全文。阅读窗口显示正文实际来源修订、知识修订、有效性及物理页范围。“打开冻结原件”
使用系统默认程序，适合核对原件版式。历史知识与失败更新所保留的旧正文会标出实际依据。

CLI 返回包含正文、修订和覆盖信息的 JSON：

```sh
openkb --kb-dir /path/to/kb --view VIEW_ID source SOURCE_ID --pages 2-3
openkb --kb-dir /path/to/kb --view VIEW_ID source SOURCE_ID \
  --knowledge-revision KNOWLEDGE_REVISION_ID --pages 3
```

API `POST /api/v1/document/source` 接受 `kb`、`hash`（来源 ID）、`view_id`、
`source_revision_id`、`knowledge_revision_id` 和可选 `pages`。无 `pages` 读取全文。
响应的 `page_range` 是实际返回的页号，`pages` 是已验证的物理总页数；请求范围内
没有记录的页在 `diagnostics` 中列出，不会凭空补正文。旧库缺少页序或处理依据时
`coverage` 为 `unknown`、物理总页数为空，保留已有页号而不猜测缺失页。

本地 PageIndex 将索引输入和图片保存在受管目录，缓存键包含原件摘要与处理策略。
全文读取和范围读取共用缓存校验与恢复。缺少页缓存时，从同一受管输入重新提取；
图片摘要不符、原件损坏或处理策略不再可重建时会明确报错。

Markdown 原文固定为 UTF-8 Unicode 码点序列，保留换行（包括 CRLF）、空白、前言、
代码和表格。去除开头 BOM 及搬移图片引用的位置由 `origin_locators` 单独映射回原文件；
`source_spans` / `char_range` 始终指向冻结正文的 0-based 半开字符范围。
图片支持 Markdown 行内/引用式链接、HTML img src 和 base64 图；代码示例不会被改写。
未能保留的图片会列入诊断，远程图片不会自动下载。

文本度量固定使用离线 tiktoken 0.13.0 / cl100k_base：5,000 tokens 及以下为短文，
与执行模型无关。`tokens` 和 `characters` 显示完整冻结文本的大小，`pages` 为空。
首次编译失败时仍可阅读已保留的规范化正文，`knowledge_revision_id` 为空且状态保留失败原因。
已有成功正文时继续显示该正文及其度量，失败的新目标另列 `target_processing`。

```sh
openkb --kb-dir /path/to/kb source SOURCE_ID --chars 0:120
```

API 对应可选字段为 `chars: "0:120"`；桌面原文窗口提供相同范围输入。
问答来源工具 `get_text_content` 返回相同字符范围和原文件映射，且受当前版本证据范围限制。
重编译复用冻结输入和分类；watch 对本地图片的更改和缺失状态也生成新的输入版本。
