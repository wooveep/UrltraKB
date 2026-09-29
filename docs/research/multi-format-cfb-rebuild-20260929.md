# 标准 DOC/XLS 嵌入子存储的 CFB 重建路径

调查日期：2026-09-29。仓库基线：`f4d7d1d34bbaf5330963accecf4f5304a9155aed`。本报告只回答：已选 LibreOffice 运行时的公开 UNO 接口能否将标准 DOC/XLS 嵌入子 storage 导出成保留必要元数据的完整 CFB；若不足，哪个已有 writer 提供可开发路径。它补充[嵌入对象调查](https://github.com/wooveep/UrltraKB/blob/01b06cfcbd303d9127f04970d0022fc1a82e84e8/docs/research/multi-format-embedded-objects-20260929.md)，不改变当前引用识别、六宿主范围、附件独立来源或父删除保留附件等既定政策。

## 结论及证据等级

**已有可开发路径：优先验证一个使用 Rust `cfb = "=0.15.0"` 的小型受监督 helper，读取冻结宿主中已识别的子 storage，将其内容树映射到新 CFB 根。** 它的公开 API 能创建 storage/stream、逐流复制，并设置 storage CLSID、状态位及时间；上游还有同类整树复制实现可作参考。这是源码支持的工程候选，不是已批准合入的依赖，也不是标准 DOC/XLS 的实样成功结论。[固定版本 API 源码](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/src/lib.rs#L703-L740)

**LibreOffice UNO 能创建和写入 CFB，不能笼统说它没有 writer；但核实到的公开 OLESimpleStorage 路线不足以直接承诺上述保留元数据的导出。** 它内部确实重建子树，可公开取得的却是容器接口而非那份重建字节；通过公开写接口再复制时，不能设置原有 CLSID，亦未提供完整目录元数据读写。未找到该版本可直接调用、可满足本契约的 UNO 子树导出入口。这里的结论限定于下述已检查接口，不声称排除了所有可能的 LibreOffice 扩展或内部 C++ 实现。[固定版本实现](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/sot/source/unoolestorage/xolesimplestorage.cxx#L357-L406)

本次仅阅读官方 SDK、固定源码、Microsoft 规范及候选库源码；未安装/构建 writer，未下载完整 LibreOffice，未运行转换、模型或 Office 样本。报告中的“建议”须进入交付契约选择；“源码支持”不等于样本验收通过。

## 1. LibreOffice 公开 UNO 路线的具体能力与缺口

沿用[运行时研究](https://github.com/wooveep/UrltraKB/blob/1894b2881d6fdf36958c3c5f97ad2eb953bb5849/docs/research/multi-format-office-runtime-20260929.md)中的 `libreoffice-26.2.6.3`，本次解析其标签到源码提交 **`8221e31b3ac356a1623c672912a3d2b492f7e3d1`**。在线 SDK 页面滚动到 26.8，故只用它说明接口名称，行为判断以该固定提交为准。[标签](https://github.com/LibreOffice/core/tree/libreoffice-26.2.6.3)、[SDK 服务定义](https://api.libreoffice.org/docs/idl/ref/servicecom_1_1sun_1_1star_1_1embed_1_1OLESimpleStorage.html)

| 已检查入口 | 固定源码事实 | 对本任务的含义 |
| --- | --- | --- |
| `com.sun.star.embed.OLESimpleStorage` | 服务注册存在；接收 `XStream` 或 `XInputStream`，支持容器、事务及分类接口 | 可通过同包 UNO 使用，不需要猜一个 PyPI UNO 包 |
| `getByName(storage_name)` | `OpenStorage → new Storage(temp_stream) → CopyTo → Commit`，之后包装为 `XNameContainer` 返回 | 内部有真实 CFB 重建能力，但调用者没有拿到内部临时 CFB 的 `XStream` |
| `getByName(stream_name)` | 复制该流数据到临时流并返回输入流 | 可读 stream 正文，不等于导出整个子 storage |
| `insertByName(name, XNameAccess)` | 递归枚举子项；只区分输入流与子 `XNameAccess` 并复制内容 | 没有复制 CLSID、state bits、目录时间的分支 |
| `getClassID` / `setClassInfo` | 前者返回 storage class ID；后者直接抛 `NoSupportException` | SDK 声明“可设置”不证明该实现可写原 CLSID |
| `commit()` | 提交 storage，并在需要时更新调用者原始 `XStream` | 可创建输出文件，但不能修复上述元数据缺口 |

源码：[服务注册](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/sot/util/sot.component#L25-L31)、[构造及原 stream 更新](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/sot/source/unoolestorage/xolesimplestorage.cxx#L45-L173)、[递归插入](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/sot/source/unoolestorage/xolesimplestorage.cxx#L215-L285)、[读取子存储/流](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/sot/source/unoolestorage/xolesimplestorage.cxx#L357-L450)、[提交与分类方法](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/sot/source/unoolestorage/xolesimplestorage.cxx#L595-L665)。

`OLESimpleStorage` 类只实现 `XOLESimpleStorage` 与 `XServiceInfo`；其原始/临时 streams 是私有成员。不能通过未声明的 `XStream` 查询或临时文件路径偷取字节，再称为稳定公开接口。[类定义](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/sot/source/unoolestorage/xolesimplestorage.hxx#L43-L62)、[固定 IDL](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/offapi/com/sun/star/embed/XOLESimpleStorage.idl)

进一步核对内部 `Storage::CopyTo`：它会复制 storage CLSID，说明 LibreOffice 自己也区别“递归流内容”与“完整 storage 复制”。但这是内部 C++ API，不是上述 UNO 导出方法；为此自行链接或修改同版 `sot` 会引入同版 native 构建、ABI及发行材料工作，本报告不将其当作现有 Python helper 可直接调用的能力。[内部复制](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/sot/source/sdstor/stg.cxx#L669-L754)、[C++接口](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/include/sot/storage.hxx#L68-L155)

不建议用“UNO 打开嵌入对象再另存为 DOC/XLS”替代字节恢复契约：`XEmbedPersist.storeToEntry` 面向嵌入对象自身的持久化和目标 `XStorage`，不是原宿主任意 CFB 子树的无损复制 API。本次没有证明它能在不激活对象、不经过文档模型重写的情况下完成所需恢复。[XEmbedPersist](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/offapi/com/sun/star/embed/XEmbedPersist.idl#L96-L132)

另有源码中已记录的约束：`oox::ole::OleStorage` 避免原位写子 storage，使用新临时 storage 后整项重新插入，注释指出原位写可能破坏无关流。无论采用何种 writer，本任务都只读冻结宿主、写独立输出，不改写宿主。[同版 oox 实现](https://github.com/LibreOffice/core/blob/8221e31b3ac356a1623c672912a3d2b492f7e3d1/oox/source/ole/olestorage.cxx#L292-L321)

## 2. 推荐候选：`cfb 0.15.0`，不依赖 Office 激活

上游：`mdsteele/rust-cfb`，作者 Matthew D. Steele；固定标签 `v0.15.0`，提交 **`e4b57666649b2ade90c1656ddae822181dd121e4`**。它是读写 CFB 的 Rust 库，不是已实现 DOC/XLS 格式识别或当前对象关系解析的提取器。[项目](https://github.com/mdsteele/rust-cfb/tree/e4b57666649b2ade90c1656ddae822181dd121e4)、[版本化 API](https://docs.rs/cfb/0.15.0/cfb/struct.CompoundFile.html)

| 所需能力 | 已存在的公开 API |
| --- | --- |
| 只读宿主、检查指定 storage | `cfb::open`、`entry`、`read_storage` |
| 仅遍历目标子树 | `walk_storage(path)`，前序且包含目标 storage 自身 |
| 创建新完整 CFB | `CompoundFile::create_with_version`，允许选择 V3/V4 |
| 保留嵌套目录与逐流字节 | `create_storage`、`open_stream`、`create_stream`，配标准 `Read/Write` 分块复制 |
| 复制目录身份/标志 | `Entry::clsid/state_bits`、`set_storage_clsid`、`set_state_bits` |
| 保存子目录时间 | `Entry::created/modified`、`set_created_time/set_modified_time` |
| 提交与独立验证 | `flush`、关闭文件后重新 `open_strict`；另用独立 reader比对 |

对应源码：[子树遍历](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/src/lib.rs#L301-L328)、[创建容器](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/src/lib.rs#L760-L837)、[CLSID与流创建](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/src/lib.rs#L973-L1032)、[状态及时间](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/src/lib.rs#L1112-L1177)、[Entry元数据](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/src/internal/entry.rs#L38-L96)。

上游 `copy_to()` 提供整文件树复制范例：先创建树及复制流，再写回元数据，最后 flush。**它不是现成的 `export_substorage()`**；项目仍需实现“选定子树路径重定位为根”与预算、验证、结果封装。无需自行实现 CFB 的目录/FAT/miniFAT分配算法。[复制范例](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/src/lib.rs#L703-L740)

许可为 MIT，要求保留上游版权与许可。manifest 声明 edition 2018、`rust-version = "1.74"`；普通依赖为 `fnv = "1.0"`、`uuid = "1"`、`web-time = "1"`。这些是上游允许范围，**不是本项目可交付的完整锁定闭包**。实施时须精确锁 `cfb`，生成并审计 `Cargo.lock`、依赖许可及源包校验；不能从 manifest 的最低 Rust声明推出任意未来解析出的传递依赖都支持相同编译器。[Cargo.toml](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/Cargo.toml)、[MIT许可](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/LICENSE)

上游 CI 定义了 Ubuntu、Windows、macOS和 stable Rust，但本次没有运行或核验这些 CI结果。该源码不要求 COM/OLE server；建议分别构建 Windows x64、Linux x86_64 helper，随包分发受监督可执行文件，用户机器不安装 Rust工具链。Linux libc/CPU基线、Windows运行库、程序散列及最终包内启动仍须按现有桌面发行要求验证，不能以“Rust跨平台”代替。[CI定义](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/.github/workflows/tests.yml)、[标准文件/IO接口](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/src/lib.rs#L48-L86)

## 3. 可开发的最小恢复算法与格式边界

以下是根据已核实 API提出的实施方案，尚未编写或执行：

1. 输入仅为冻结宿主引用、已识别的目标 storage path、处理指纹、预算和私有输出位置。当前引用识别由 DOC字段/XLS Obj映射负责；writer不得扫描出全部 storages后自行认定附件。
2. 只读打开宿主，确认路径是 storage，取得该 storage及其所有后代的稳定清单。只处理当前直接对象，不在此 helper递归派发附件。
3. 创建全新 CFB；使用宿主 CFB版本作为首选保留策略，版本兼容性纳入样本。目标 storage本身映射为 `/`，后代相对路径保持；先创建目录，再逐流有界复制。包括空流、mini streams、大流及嵌套 storage；不只挑 `WordDocument` 或 `Workbook`。
4. 树构造完成后复制所选 storage的 CLSID、state bits到新根；复制后代 storage的 CLSID、state bits及有效时间。**所选 storage提升为根后，根 Creation Time必须为0；源子storage原时间另存恢复元数据，不机械复制到根。** 流CLSID和时间仍服从规范的0值要求。其余流字节原样保留，不启动嵌入应用。
5. flush并关闭输出，严格重开验证 CFB结构；比较每个相对stream路径、长度和hash，以及目录元数据。再做标准DOC/XLS结构与下游reader检查，只有两层都成功才标标准文件恢复成功；CFB有效本身不证明它是Word/Excel文件。
6. 输出完整文件及恢复清单，交回既有发现检查点统一保存payload、关系、子意图和预算。错误返回具体阶段；未验证成功的对象继续保留原宿主及storage定位，不改后缀冒充成功。

CFB规范依据：storage/root允许CLSID及state bits，stream CLSID/时间有0值约束；根名称、对象类型、mini-stream职责与普通storage不同，根Creation Time必须为0。因此恢复是逻辑子树导出，不是把原目录项字节原样当新根，更不是截取连续物理扇区。[MS-CFB §2.6.1](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-cfb/60fe8611-66c3-496b-b70d-a504c94c9ace)、[§2.6.2](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-cfb/026fde6e-143d-41bf-a7da-c08b2130d50e)

CLSID为0在CFB规范中也可能合法；本报告不声称丢失非0 CLSID必然让所有DOC/XLS打不开。要求保留已知元数据，是为了不在恢复过程中无声改变对象身份。writer处理不了的异常名称、损坏结构或元数据须明确诊断，不能默认修好或跳过后仍声明完整恢复。

时间戳还需一个明确验收：`cfb`的公开 Entry时间转换经过`SystemTime`，源码说明极端值在某些平台可能不可表示。两目标平台应比对实际FILETIME往返；无法保真的值保留原始证据并报告，不作跨平台任意时间值的绝对保证。[时间转换源码](https://github.com/mdsteele/rust-cfb/blob/e4b57666649b2ade90c1656ddae822181dd121e4/src/internal/timestamp.rs#L53-L91)

## 4. 本能力的最小验收与交付门槛

无需重新扩大格式研究，但在宣布原生子storage恢复可用前至少完成：

- **两个真实宿主**：DOC ObjectPool含原生Word子storage；XLS MBD含原生Excel子storage。至少一例包含nested storage、非0 CLSID、mini stream、大于mini cutoff的stream及0长度stream；不必每个宿主重复全部边界。
- **内容及结构**：每个stream的相对路径、长度、hash与原宿主对应子树一致；保留子storage元数据，目标根按规范变换；输出能被独立CFB reader重开。容器整体hash可不同，不拿它冒充嵌入前文件的历史字节。
- **标准格式**：独立DOC/XLS reader能读出预置文字/单元格；同时验证所有必要streams存在。此检查不能只看CFB magic或只让writer自己重开。
- **拒绝与隔离**：缺失目标、stream冒充storage、损坏FAT/miniFAT、预算超限、私有/ActiveX子树，返回准确状态；宿主字节不变；一个对象失败不阻断其他对象。
- **发行物**：在最终Windows/Linux应用包中调用helper，验证进程退出、超时/取消、实际输出字节预算、中文/空格路径及依赖清单。上游CI定义不能代替这一项。

因此 `requires_container_rebuild` 不再是“没有候选路径”的研究空白，但在helper及上述样本验收完成前仍是准确能力诊断。是否采用此Rust helper、依赖闭包和样本门槛属于交付决策；本报告不批准依赖合入，不运行恢复，不关闭票据。

研究访问使用agent-reach的GitHub/gh CLI和Jina Reader；Exa未配置时用内置网页搜索定位官方API。仅下载少量固定源码文本到`/tmp`供核对；未写业务代码。Agent Reach版本检查为v1.5.0，报告时无更新提示。
