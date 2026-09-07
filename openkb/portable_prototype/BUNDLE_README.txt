OpenKB · 免安装运行探针（可丢弃）

这是技术原型，不是桌面产品。Linux 包只用于 Debian 13.6 x86_64 GNOME/X11。
解压整个目录，双击 OpenKBProbe；Windows 目标原生构建后双击 OpenKBProbe.exe。
不要单独复制可执行文件。Linux 解压需要保留符号链接和执行权限。

启动后自动运行真实本地文档处理、隔离工作进程及 32 项渲染样本（明暗主题）。
模型传输使用本机虚拟响应，不调用真实模型、不需要填入 API Key。
默认在系统临时目录创建 OpenKB-PROTOTYPE-* 测试目录和独立配置，关闭后保留以便核查。
这只是原型的隔离措施；正式程序仍按已确定的既有配置路径设计。

窗口按钮：关闭窗口（有托盘时隐藏，无托盘时保持可见）；真正退出（等待当前阶段收尾）。
列表可切换随包生成的 PNG。下方记录工作进程、转换及失败详情。
本原型的“请求安全停止”只在专门的逐项写入探针生效，其余工作会完成当前阶段。

结果：输出目录 summary.json、window.png、各任务子目录。
命令行可指定独立输出位置：OpenKBProbe --output "某个新建的空目录"
自动记录后退出：OpenKBProbe --output "某个新建的空目录" --auto-exit
--use-system-profile 仅供干净测试账户/容器验收，会在该账户的原位置登记测试知识库。

程序由 PyInstaller onedir + Python 3.12.13 + Qt/PySide6 6.11.2 构建；
携带 Node 24.20.0、MathJax 4.1.3、Merman 0.7.0 和 resvg 0.46.0。
窗口不使用 WebView，内容渲染不使用后台浏览器引擎。

此包仅供当前方案的本地技术验证。正式分发材料与 PyMuPDF/MuPDF 再分发安排另有决策票。
