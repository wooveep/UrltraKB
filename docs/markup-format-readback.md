# HTML / XML 的冻结与来源读取

本地 HTML/HTM 与 XML 文件使用共同来源接入、版本判定、固定 token 分类、
内容块索引及编译路线。CLI、API、桌面和监听读取同一份规范化正文。回读只用
冻结原件、映射及资产，不重新连接网络或读取原来的用户文件。

HTML 使用已锁定的 Beautiful Soup `html.parser` 和 Markdownify，保留标题、
段落、强调、列表、表格、代码、超链接和图片。脚本、样式与页面 head 不作为
正文；既不执行脚本，也不下载样式或脚本来运行网页。跨块链接转换为各块内的
链接，标题及表格内的图片仍保留引用。`base` 参与图片和超链接目标解析。
解码原件的 Unicode 码点半开区间与原始 DOM 路径记录在映射中；一个转换片段
可以包含多段正文，其原始范围保守保留整个片段，不声称逐字符等长对应。

图片支持文档目录内的本地引用、base64 或 URL 编码的 data URI，以及明确启用
后的 HTTP(S) 下载。每条图片记录原始引用、解析目标、原件范围和获取状态：
`retained`、`missing`、`not_requested`、`failed`。成功资源包含受管路径与内容
散列；失败保留原链接及诊断，HTTP 协议中断不会令已取得正文变成空内容。
无法解析的协议相对 URL 保留为链接并报告获取失败，不猜原页面协议。

`download_remote_assets` 默认为 false，优先级为单次 > 本库显式值 > 全局 > false。
显式 false 不被上层 true 覆盖，配置 PATCH 中 null 清除该层覆盖。

- CLI：`openkb add guide.html --download-remote-assets` 或 `--no-download-remote-assets`。
- API：`POST /api/v1/add` multipart 字段 `download_remote_assets=true/false`；普通及 SSE 导入一致。
- 桌面：导入表单的「本次 HTML 远程图片」可继承、下载或仅保留链接；设置页支持本库与全局值。
- 监听：采用执行时捕获的本库/全局配置。本地图片改变会形成新的来源修订。

单个远程图片使用 15 秒网络超时和 20 MiB 大小限制，仅接受声明为受支持图片的
响应。规范化文本、映射、实际策略、策略来源及资产散列共同进入冻结指纹；
块输入包也携带这些信息。来源窗口显示实际策略、状态和诊断。URL 主文提取
仍走已有 trafilatura 路线；上述选项针对 HTML 文件导入。

XML 仅验证结构并以代码围栏保留解码原文：声明、CDATA、注释、属性及换行均
保留，不推断业务类型，不重排或重写 XML。defusedxml 禁止 DTD、实体和外部
实体。编码优先 BOM；无 BOM 时识别 XML 的常用 UTF-16/32 字节签名及 ASCII
兼容声明，声明与实际编码冲突时失败并保留诊断。签名依据
[XML 1.0 Appendix F](https://www.w3.org/TR/xml/#sec-guessing-no-ext-info)。
其余文字编码沿用严格解码流程；原件与编码决定先于格式解析保存，失败不发布空知识。

直接调用的 `beautifulsoup4==4.14.3`、`markdownify==1.2.2`、`defusedxml==0.7.1`
均从现有锁文件提升为直接依赖，未改变版本；已核对随发行包携带的许可证，分别
为 MIT、MIT、PSF。没有新增同类解析框架。

自动验证覆盖实际小文件的导入、冻结、发布、字符/块读取、资源失败、策略层级、
监听资产变更和真实 Qt 显示；外部模型与网络在边界替换。真实模型质量和最终
发行包的 Windows / Debian 验收仍由对应人工验证议题确认。
