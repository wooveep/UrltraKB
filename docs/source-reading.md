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
