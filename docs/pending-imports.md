# 自动文件恢复与待办

文档接入先冻结原件和发现意图，正文独立处理。发现阶段逐个冻结完整文件、普通导入意图、游标和预算；同一实际对象的多处引用只产生一次导入。恢复的文件是普通来源，使用自身内容进行版本判断，不继承容器的产品、版本或查询关系。删除容器不撤销已经提交的文件和待办。

DOCX、PPTX、XLSX 从原始 package 扫描嵌入目录、内容类型和 package/OLE 关系目标，包含图表工作簿、自定义路径和无正文引用的残留文件。同一实际 part 的多处引用不重复导入；不同 part 即使内容相同仍是不同来源。ZIP 内容类型、PDF 和 CFB 流用于识别真实文件类型，支持标准 Package/Ole10Native 包装中的完整文件。原始 OOXML、PDF、标准 DOC/XLS/PPT 和经过解码验证的支持文本按普通来源接入。

待办检查点分别显示私有对象、预览图、外链（不下载）、损坏对象、循环和需要标准容器重建的对象。不能恢复的对象保留原件和技术定位，不冒充已导入文档。旧 DOC 的标准 CFB 子 storage 由[随包 helper](cfb-helper.md)重建并独立核对后普通导入；未提供重建能力时明确报告 `requires_container_rebuild`。

CLI 正文结束显示发现和文件导入待办数。显式排空可运行工作：

```bash
openkb process-pending --kb /path/to/kb
openkb pending list --kb /path/to/kb
openkb pending retry JOB_ID --kb /path/to/kb
openkb pending budget GROUP_ID max_sources=200 --kb /path/to/kb
openkb pending cancel-group GROUP_ID --kb /path/to/kb
```

命令完成后退出，没有隐式后台进程。桌面在打开的知识库中逐个派发待办，显式用户任务优先。资料管理的“查看待处理工作”可以查看两种工作状态、调整执行组预算、显式重试或取消执行组。任务列表的停止只停止当前任务；取消执行组停止本组所有未完成工作，保留已完成知识。

每个根导入冻结一组预算：默认深度 8、来源数 100（包括根来源）、单对象 32 MiB、累计恢复文件 256 MiB、累计解压 512 MiB、发现时间 30 秒。全局或知识库的 `extraction_budget` JSON 影响之后的新执行组。待办界面显示当前组的预算来源及消耗；提高预算后，等待工作用新 attempt 从保存的位置继续。循环检测按当前恢复路径上的内容摘要停止递归，不合并不同实际对象的来源身份。

未启动的投递可以重新认领，旧投递令牌失效。worker 在业务调用之前保存 started 和 attempt。异常重启后，已提交的业务结果优先于任务回执；确实未知的已启动工作需要显式重试，可能重复模型调用和费用。失败、版本等待、预算等待、已停止和已取消的工作不会因为轮询自动重新运行。清除任务历史不会删除业务待办。

HTTP API 共用应用服务：`GET /api/v1/pending`，以及该前缀下的 `process-pending`、`retry`、`cancel-group`、`budget` POST 动作；沿用既有鉴权和知识库定位规则。
