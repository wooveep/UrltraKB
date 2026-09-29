# 内嵌 Office 对象与图表工作簿：字节恢复能力调查

调查日期：2026-09-29。对应研究票：[《研究：内嵌 Office 对象与图表工作簿能可靠恢复到哪些标准文件？》](https://github.com/wooveep/UrltraKB/issues/68)。仓库基线：`f4d7d1d34bbaf5330963accecf4f5304a9155aed`。这是规范、锁文件及候选库源码调查，不是已实现能力或样本验收结论。

沿用已定语义：从 DOC/DOCX、PPT/PPTX、XLS/XLSX 原件恢复可识别附件；附件是独立 Source，正文失败不阻断附件；递归受预算约束；内嵌图表 workbook 标记 `chart_data`；删除父 Source 只解除关联、附件全部保留；无法重建的私有对象明确不支持。本文不从派生 PDF 寻找附件，不调整这些政策。

## 结论与证据等级

**可靠恢复的单位必须是完整文件字节，不能把“发现 OLE stream”算作“恢复 Office 文件”。** 有三类不同工作：

1. **直接提取已有完整文件**：OOXML 内嵌 package part；CFB 中承载完整 OPC 文件的 `Package` stream；经过识别的 OLE Package 包装中的原始文件数据；PPT `ExOleObjStg` 中已完整序列化的 CFB，解压后再判断它是否确实是目标标准文档。微软规定 `Package` stream 可直接复制成 embedded package part；PPT 压缩字段承载 structured storage。[MS-OI29500 §3.10](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-oi29500/32be961f-d71a-4812-913f-b675c79aa88a)、[MS-PPT §2.10.36](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/305e541f-2c91-49c5-a742-4955330fd2b9)
2. **重建容器后才可能得到标准文件**：DOC `ObjectPool/_…`、XLS `MBD…` 中的原生 Word/Excel 等子 storage。它们可能包含标准 Office streams，但子 storage 不是一段完整 CFB 文件；CFB 的目录、FAT、mini stream 属于外围文件。需要把子树复制到新的有效 CFB 根并保留必要元数据，再验证文档格式。`olefile` 不提供此类新建/导出 API。[MS-CFB §2.6](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-cfb/a94d7445-c4be-49cd-b6b9-2f4abc663817)、[§2.6.2](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-cfb/026fde6e-143d-41bf-a7da-c08b2130d50e)、[olefile 0.47 源码](https://github.com/decalage2/olefile/blob/ae8e110e90965f2648052b29f1b3da2c39465001/olefile/olefile.py#L1990-L2014)
3. **仅保留原始对象证据**：私有应用 streams、ActiveX、只剩预览/缓存的对象等。OLE 规范明确允许创建应用私有的 native streams；即使成功解析 CFB，也不说明能恢复 DOC/XLS/PDF 等文件。[MS-OLEDS §1.3.1](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-oleds/2677fcf2-ad48-4386-ba8f-b1b7baf4c02f)

以下“可恢复”表示规范和源码支持这条路径，**均未在本调查中通过真实 Office 样本运行验证**。对于容器重建和位置恢复，本文分别标明缺口。

## 能力矩阵

| 宿主/结构 | 能获得的字节与状态 | 候选读取路径 | 最稳妥的原件位置 | 明确边界 |
| --- | --- | --- | --- | --- |
| DOCX/PPTX/XLSX 的内置 `package` 关系目标 | 目标 part 本来就是完整文档时直接恢复标准文件 | `zipfile` + relationship/XML；读取原 part 字节 | 宿主 part、关系 ID、引用元素；再关联段落/shape/sheet | 不按 `embeddings/` 目录名或扩展名认定类型 |
| OOXML 的 `oleObject` 目标 CFB part | 完整 CFB 可原样保留；内部有 `Package`/可识别 Ole10Native 时再解包；原生标准 CFB 需验证后认定 DOC/XLS 等 | ZIP + `olefile`，必要时 `OleNativeStream` | 同上；嵌套对象再加 storage/stream 路径 | `.bin` 不等于不可恢复，CFB magic 也不等于标准 Office 文件 |
| DOCX/PPTX chart → 内置 XLSX | 原 workbook 字节可直接恢复，角色 `chart_data` | chart part 的 `c:externalData/@r:id` → chart `.rels` 的 `package` 目标 | 原引用位置 + chart part + workbook part | 图表缓存不等于 workbook；外链不获取 |
| XLSX 自有图表引用当前工作簿 | 通常没有另一个内嵌 XLSX 可提取 | 图表 `c:f` 引用当前工作簿数据 | sheet/drawing/chart | 不能为了“恢复”而把缓存/单元格另存新 workbook |
| DOC `ObjectPool/_…` 内 `Package` 或可识别 Ole10Native | 提取其中完整文件字节 | `OleFileIO.listdir/openstream` + 包装解析 | storage 路径；有额外 DOC 字段解析才有 CP | 子 storage 名不表示页码或顺序 |
| DOC `ObjectPool/_…` 内原生 Office 多 streams | **需要重建 CFB**；现候选不能直接导出标准文件 | `olefile` 只能读子树 | storage 路径 | 单独输出 `WordDocument` 或 `Workbook` stream 不是恢复 DOC/XLS |
| PPT `ExOleObjStg` 压缩/未压缩 | 能提取完整对象 storage 字节；再分为标准 CFB、包装文件或私有对象 | `PptRecordExOleVbaActiveXAtom.iter_uncompressed()` | `PowerPoint Document` stream + record offset；补引用链后到 slide/shape | record type `0x1011` 还用于 VBA/ActiveX；扫描不等于有效附件清单 |
| XLS `MBD` + 八位十六进制子 storage | Package 可直接提取；原生 Office 子树需要重建 | `olefile` + 独立 BIFF/OfficeArt 映射 | storage 路径；补 `Obj` 映射才有 sheet/object | 不能仅因名为 MBD 就作为普通附件，可能是 ActiveX |
| 任一宿主内未知 native streams/仅 OLE presentation | 原始 part/对象证据，准确报不支持标准文件恢复 | 保留容器与对象定位信息 | part/storage/record | 不改后缀冒充标准文件，不执行 OLE server |

矩阵依据：OOXML 对象节点的关系语义见 [Word OLEObject](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.vml.office.oleobject?view=openxml-3.0.1)、[PowerPoint oleObj](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.presentation.oleobject?view=openxml-3.0.1)、[Excel oleObject](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.oleobject?view=openxml-3.0.1)；图表见 [ISO/IEC 29500 的 ExternalData 定义](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.drawing.charts.externaldata?view=openxml-3.0.1)；二进制结构见后两节具体规范和源码。

## OOXML：先恢复关系，再读取 payload

### 普通对象与图表的路线不同

通常路径如下；实际路径必须由 relationship 解析，不能固定假设文件编号或目录：

```text
DOCX：word/document.xml、header/footer 等宿主 part
      → o:OLEObject/@r:id（常见 Transitional 路线）
      → 该 part 对应的 .rels → embedded part

PPTX：presentation 的 slide 列表 → slide part
      → graphicFrame 中 p:oleObj/@r:id → slide .rels → embedded part

XLSX：workbook 的 sheet 列表 → worksheet part
      → oleObjects/oleObject/@r:id → worksheet .rels → persistence part

DOCX/PPTX 图表：宿主 drawing/graphicFrame → c:chart/@r:id
      → chart part → c:externalData/@r:id
      → chart .rels 中 package 关系 → 内置 XLSX part
```

`externalData` 可指外部文件，也可指同一 package 中独立的 SpreadsheetML 文档；规范规定其关系类型为 `…/relationships/package`。因此 `externalData` 的名字不能被误读为一定外链。Excel 自己的图表可通过公式引用本 workbook，规范特别说明这种场景不使用上述元素。[ExternalData](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.drawing.charts.externaldata?view=openxml-3.0.1)

较新的 ChartEx 另有 `http://schemas.microsoft.com/office/drawing/2014/chartex` 命名空间和 `CT_ChartData/externalData`，同样可能含内置 Spreadsheet 对象；仅识别传统 `c:chartSpace` 不能宣称覆盖所有 Office 图表。本次验证了标准事实，未核实候选库对所有 ChartEx 类型的加载覆盖。[MS-ODRAWXML §2.24.3.10](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-odrawxml/8a963284-245d-4c2a-935d-cc6aef448b09)

### 可核验的关系与 occurrence 记录

下面是根据 OPC 关系模型推导的后续实施约束，不是新产品政策：

- `rId` 只在所属关系集合中解释。关系键应包含 `owner_part + relationship_id`，目标 URI 按所属 part 解析；`TargetMode=External` 只记录，不访问。不能把所有 `.rels` 合成一个全局 rId 字典。[OPC part relationships](https://learn.microsoft.com/en-us/dotnet/api/system.io.packaging.packagepart.createrelationship?view=windowsdesktop-9.0)、[python-pptx 1.0.2 的包加载及目标解析](https://github.com/scanny/python-pptx/blob/v1.0.2/src/pptx/opc/package.py#L231-L267)
- 文件字节可以按目标 part/内容摘要去重，**每个引用元素仍保留 occurrence**：宿主 part、元素路径或稳定标识、局部 rId、完整关系链、目标 part、位置种类与可用坐标。两处引用同一 chart、两个 chart 共用 workbook、同一 workbook 同时用于普通附件和 `chart_data`，都不能因文件去重丢失关系用途。OPC 允许同一 target 参与多条关系；这里的 occurrence 结构是本报告建议。[Microsoft 的 OOXML Packages 说明](https://download.microsoft.com/download/E/1/4/E14FB96F-83B8-4A2A-84DB-7FA8ACBE061A/tc45-2006-334.pdf)
- 预览图按其 image 关系和形状用途识别，不能成为 OLE 文件成功恢复的证据。Word 的官方示例将 `v:imagedata/@r:id` 与 `o:OLEObject/@r:id` 分开；OLE2 的 `\x02OlePres…` 是 presentation data。[Word OLEObject 示例](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.vml.office.oleobject?view=openxml-3.0.1)、[MS-OLEDS §1.3.1](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-oleds/2677fcf2-ad48-4386-ba8f-b1b7baf4c02f)
- “有 relationship”与“正文元素正在引用”分开记录；没有可追溯 occurrence 的 package part 不能伪造段落或页码。可识别的孤立 payload 应保留原件、对象与 `orphan/unreferenced` 未关联状态；自动导入行为由交付契约明确，不能只扫描 ZIP 文件名后宣称它出现于正文或伪造 occurrence。存在 XML `AlternateContent` 时还要防止 Choice/Fallback 形成重复 occurrence；具体兼容分支解析尚未验证。
- 类型判定同时参考关系、Content Types 和原始字节结构。内嵌 OPC 文档核对 `[Content_Types].xml`、根 officeDocument 关系及 main part；`ProgID`、文件名仅作线索。Strict 与 Transitional 的关系 URI 不同，不能只写一个 URI 字面量后宣称全覆盖。[MS-OI29500 §3.10](https://learn.microsoft.com/en-us/openspecs/office_standards/ms-oi29500/32be961f-d71a-4812-913f-b675c79aa88a)

### 不编造位置

| 容器 | 可以准确表达 | 不应从提取结果推定 |
| --- | --- | --- |
| DOCX | part + XML 引用位置，段落/表格单元格路径；header/footer/footnote 等 story 单独标明 | 渲染页码；页眉对象不天然只有一次页面出现 |
| PPTX | 按 presentation slide 顺序恢复的 slide 标识/序号，graphicFrame/shape 标识；notes/master 标明所属 part | `slide17.xml` 必然是第 17 页；母版对象在每页的渲染 occurrence |
| XLSX | workbook sheet 名/ID + worksheet part + `shapeId`；若能连到 objectPr/drawing/VML anchor，再报告覆盖区域/左上角单元格 | 只有 shapeId 时推测 cell；把“覆盖 A1 的浮动对象”写成“A1 单元格值” |

上表中位置降级是保守推论。XLSX 的对象关系 ID 指向 persistence part，shapeId 则关联图形；openpyxl 的 `ObjectAnchor` 源码存在 from/to marker，但不能据此推定其 workbook API 已完成附件解析。[Excel oleObject](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.spreadsheet.oleobject?view=openxml-3.0.1)、[openpyxl 官方源码文档](https://openpyxl.readthedocs.io/en/stable/_modules/openpyxl/worksheet/ole.html)

## OLE/CFB：Package、Ole10Native 与子 storage 不可混称

`Package` stream 是一种 Office 保存 OPC 文档的承载方式；**OLE Package 对象**则是另一种包装。`\x01Ole10Native` 在 MS-OLEDS 中只保证 `NativeDataSize + NativeData`，后者可以是创建应用私有数据，不保证都带文件名，也不保证都是可独立打开的文件。微软的 OLENativeStream 示例甚至直接以 BMP native data 展示，说明不能对所有该名 stream 强套 Package 字符串布局。[MS-OLEDS §2.3.6](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-oleds/cc825ec3-f0ed-4023-97f6-13c323ac1172)、[§3.5 示例](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-oleds/6a6e5413-e544-41c5-bd2c-cfa84bacd781)

`oletools.oleobj.OleNativeStream(data, package=False)` 的实际实现读取 size、短整型、零终止 filename/source path、两个整数、临时路径、actual_size 和 payload；`package=True` 只跳过最前面的 native size，并不表示通用 OOXML package parser。它还有 permissive/strict TODO，读不到 actual_size 时可能归为 link。故需先识别匹配的包装、验证长度和文件结构，不能把“解析没有异常”当作可靠恢复。[oleobj.py 351–435](https://github.com/decalage2/oletools/blob/b565533d6757f3dde2501e946ed6977ebbf2830a/oletools/oleobj.py#L351-L435)

对未支持对象，“保留原始对象”应准确区分：OOXML 独立 `.bin` part 和 PPT 序列化 CFB 能直接保留原字节；DOC/XLS 子 storage 无独立原始文件字节，至少可保留原宿主、完整 storage 路径和所需 streams 证据。把 streams 装进 ZIP 可作诊断归档，**不能命名为恢复后的 DOC/XLS**。这是由 CFB storage/stream 层级推导的边界。[Microsoft Storages and Streams](https://learn.microsoft.com/en-us/windows/win32/stg/storages-and-streams)

## 三种二进制宿主的定位成本

### DOC：ObjectPool 与正文 CP

MS-DOC 明确规定 ObjectPool 下存放嵌入对象 storages，名称来自对应 OLE 字段分隔符字符的 `sprmCPicLocation` 值转十进制字符串并加 `_`；要从正文定位，须经过字段类型/CP、直接字符格式、字段表这条链。`\x03ObjInfo` 提供对象信息，不是原文件 payload。`olefile` 仅列出 ObjectPool 子树不会恢复该链；首个确实可报告的位置是 storage path，CP 需要额外 DOC 解析，页码需要布局依据。[MS-DOC §2.1.4](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/f7983581-d107-4a1f-b5f7-f3650e777c04)、[§2.1.4.1](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/13ba10a8-d8b2-433b-bf3b-ec238dc8f9ce)

### PPT：记录解压成功仍未完成引用恢复

`ExOleObjStg` 的 `recInstance=0` 表示未压缩，`1` 表示压缩；压缩记录先有 4 字节 decompressedSize，其后为 zlib 包装的 DEFLATE 数据。规范的当前文档恢复流程要求根据 `Current User` / edit / persist directory 解析活记录，再从 `ExOleEmbedContainer.exOleObjAtom.persistIdRef` 定位对象 storage。[MS-PPT §2.10.34](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/21e29c16-df3a-4352-8017-2c48864d2548)、[压缩结构](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/305e541f-2c91-49c5-a742-4955330fd2b9)、[PowerPoint Document Stream，Part 9](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/1fc22d56-28f9-4818-bd45-67c2bf721ccf)

shape 的 `OfficeArtClientData.ExObjRefAtom` 关联外部对象 ID，之后才能结合当前 slide 内容形成 occurrence。官方 OLE 示例给出了 exObjId、persistIdRef、record offset 之间的对应。[OfficeArtClientData](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/ac3b454d-b5ea-4da8-a57c-32fc08ed332a)、[OLE Object 示例](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-ppt/ff524da9-1f4d-4df5-91e8-7a734530ad11)

oletools 的 `find_ole_in_ppt()` 扫描记录并 yield `OleFileIO`，不输出上述完整引用链；`PptRecordExOleVbaActiveXAtom` 注释明确留下区分 OLE/VBA/ActiveX 需要查对应 `ExOleObjAtom` 的 TODO。底层 `record.pos` 可记录流内偏移；高层生成器会关闭句柄，必须在迭代期间完成字节读取。扫描也不能保证剔除旧编辑遗留记录。[oleobj.py 614–654](https://github.com/decalage2/oletools/blob/b565533d6757f3dde2501e946ed6977ebbf2830a/oletools/oleobj.py#L614-L654)、[ppt_record_parser.py 588–679](https://github.com/decalage2/oletools/blob/b565533d6757f3dde2501e946ed6977ebbf2830a/oletools/ppt_record_parser.py#L588-L679)、[record_base.py 222–252](https://github.com/decalage2/oletools/blob/b565533d6757f3dde2501e946ed6977ebbf2830a/oletools/record_base.py#L222-L252)

还有预算缺口：`iter_uncompressed(chunk_size=4096)` 的 chunk_size 是压缩输入块尺寸，输出块可能更大；实现未向 `decompress()` 传 max_length，最终尺寸不符只 warning。采用 API 时仍需实现输出预算、长度校验及失败状态，不能把这个参数当作解压内存上限。[同一解压实现](https://github.com/decalage2/oletools/blob/b565533d6757f3dde2501e946ed6977ebbf2830a/oletools/ppt_record_parser.py#L631-L665)

### XLS：MBD 不自带 sheet/cell

MS-XLS 要求 `MBD` 加八位十六进制名称对应一个 sheet substream 的 `Obj`；`cmo.ot=8`、`pictFlags.fPrstm=0`、`fDde=0` 才符合该 storage 路径，`pictFmla.lPosInCtlStm` 给出对应 MBD 标识。MBD 也可存 ActiveX；stream-based persistence 另可落入 `Ctls`，不能一律视作内嵌文件。[MS-XLS §2.1.7.5](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xls/b406ade0-fb1c-4512-bff2-b576fdfff545)、[FtPictFmla](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xls/00f89d32-67b0-408e-9eaf-f4fecbddb089)

知道 MBD 名称仍不知道所属 sheet；需从 BIFF 子流中的 Obj 反向连接。进一步的单元格覆盖范围来自关联 drawing 的 `OfficeArtClientAnchorSheet`，字段包括 colL/rwT/colR/rwB 及偏移。锚点描述浮动图形边界，不是单元格内容。[OfficeArtClientAnchorSheet](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xls/fd656a2c-d5ee-4171-8f65-17a08b9f2262)

锁定 xlrd 2.0.2 的 `Sheet.handle_obj()` 只处理部分子记录，未实现该 MBD 恢复链；其官方文档明确将 charts/macros/pictures/embedded objects（包括嵌入 worksheet）列为忽略项目。不能因为函数名包含 `obj` 就认定已支持附件。[xlrd 2.0.2 源码](https://github.com/python-excel/xlrd/blob/2.0.2/xlrd/sheet.py#L1907-L1973)、[xlrd 文档](https://xlrd.readthedocs.io/en/latest/)

## 依赖事实与最小候选组合

### 当前锁内已有能力

在上述仓库基线逐项解析 `pyproject.toml`、`uv.lock`，没有安装或升级包。直接声明是 `markitdown[docx,pptx,xlsx,xls]==0.1.5`；Office extra 经锁文件带入下表，当前没有 `olefile`/`oletools`。非 PDF/Markdown 正文转换调用 MarkItDown，并无独立附件提取步骤。[pyproject.toml](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/pyproject.toml#L36)、[uv.lock](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/uv.lock)、[converter.py](https://github.com/wooveep/UrltraKB/blob/f4d7d1d34bbaf5330963accecf4f5304a9155aed/openkb/converter.py#L251-L272)

| 现有精确版本 | 能承担什么 | 不能据此承诺什么 |
| --- | --- | --- |
| `python-pptx==1.0.2` | 普通对象的 `shape.ole_format.blob` 返回 related part 原字节；chart part 的 `chart_workbook.xlsx_part.blob` 可取内置 workbook | 不解析 `.ppt`；blob 可能是 OLE wrapper；内部 chart property 未保证所有 ChartEx/Strict 变体；外链 `related_part` 不可直接取 |
| `openpyxl==3.1.5` | workbook/cell 读取；XML 类可作结构参考；恢复后 XLSX 的有限检查 | `worksheet.ole.OleObject` 类存在不等于 `load_workbook` 有附件导出 API；3.1.5 的 WorksheetReader 未绑定 oleObjects，OleObject 类也未声明 r:id 字段 |
| `xlrd==2.0.2` | 已恢复独立 XLS 的单元格读取 | 不导出 MBD 附件，不完成 sheet/object/anchor 映射 |
| `defusedxml==0.7.1`、`lxml==6.1.1` | 可复用现有 XML 解析依赖，配合标准库 ZIP 处理 relationships 与 Content Types | 不提供 OLE/CFB 恢复语义；若直接 import 现有传递依赖，应在实施时明确精确直接声明 |
| `mammoth==1.11.0`、`markitdown==0.1.5` | 现有 DOCX 等正文转换链 | 此处没有核验出可用于六宿主完整附件恢复的 API；不把文本转换成功计为附件成功 |

源码证据：[python-pptx `ole_format.blob`](https://github.com/scanny/python-pptx/blob/v1.0.2/src/pptx/shapes/graphfrm.py#L132-L160)、[`ChartWorkbook.xlsx_part`](https://github.com/scanny/python-pptx/blob/v1.0.2/src/pptx/parts/chart.py#L55-L95)、[外链 target_part 会拒绝读取](https://github.com/scanny/python-pptx/blob/v1.0.2/src/pptx/opc/package.py#L703-L734)。openpyxl 的 stable 文档实际标题为 3.1.3，因此另下载**锁文件指向的 3.1.5 源码包**（186,464 字节）并核对 SHA-256 `cf0e3cf56142039133628b5acffe8ef0c12bc902d2aadd3e0fe5878dc08d1050`，只读 `worksheet/ole.py`、`worksheet/_reader.py`、`reader/excel.py`，确认上述能力边界。[官方 3.1.5 源码包](https://files.pythonhosted.org/packages/3d/f9/88d94a75de065ea32619465d2f77b29a0469500e99012523b91cc4141cd1/openpyxl-3.1.5.tar.gz)

### 新增候选的精确版本和 API

| 候选 | 上游与许可 | 已核实 API | 仍缺什么 |
| --- | --- | --- | --- |
| `olefile==0.47`，tag commit `ae8e110e90965f2648052b29f1b3da2c39465001` | decalage2/olefile；Philippe Lagadec；BSD 两条款并保留 PIL 原许可；setup 未声明运行时依赖 | `OleFileIO`、`listdir(streams=True, storages=True)`、`openstream`、`get_type/get_size`、`parsing_issues`；`write_stream` 仅等长覆盖已有 stream | 不创建新 CFB，不导出子 storage 为完整文件，不恢复 DOC/PPT/XLS 位置关系 |
| `oletools==0.60.2`，tag commit `b565533d6757f3dde2501e946ed6977ebbf2830a` | decalage2/oletools；Philippe Lagadec；主体 BSD 两条款，thirdparty 各自许可，部分代码有 MIT 归属 | `oleobj.OleNativeStream` 包装解析；`find_ole`/`find_ole_in_ppt` 返回 OLE readers；`PptFile` 与 `PptRecordExOleVbaActiveXAtom` 解析/解压 PPT 记录 | 不是通用标准文件恢复器；无 CFB 子树重建；不完成 current-record/slide/shape 关系；需要外层预算与校验 |

版本为调查时 GitHub release API 返回的稳定发布：olefile v0.47 发布于 2023-12-04，oletools v0.60.2 发布于 2024-07-02；版本与时间只说明发布事实，不代表兼容性/维护质量背书。[olefile release](https://github.com/decalage2/olefile/releases/tag/v0.47)、[oletools release](https://github.com/decalage2/oletools/releases/tag/v0.60.2)

依赖与许可原文：[olefile setup](https://github.com/decalage2/olefile/blob/ae8e110e90965f2648052b29f1b3da2c39465001/setup.py)、[olefile LICENSE](https://github.com/decalage2/olefile/blob/ae8e110e90965f2648052b29f1b3da2c39465001/LICENSE.txt)、[oletools setup](https://github.com/decalage2/oletools/blob/b565533d6757f3dde2501e946ed6977ebbf2830a/setup.py#L325-L339)、[oletools LICENSE](https://github.com/decalage2/oletools/blob/b565533d6757f3dde2501e946ed6977ebbf2830a/LICENSE.md)。oletools 的运行依赖声明包含 `pyparsing>=2.1.0,<4`、`olefile>=0.46`、`easygui`、`colorclass`、`pcodedmp>=1.2.5`，以及带平台 marker 的 `msoffcrypto-tool`（普通 CPython 会选中）；完整安装会带入比附件解析更多的能力。不能用 `--no-deps` 把它描述成只有两个小包。

**最小候选判断：** OOXML 的原始 part 和 chart workbook 路线不需增加转换框架；已有 ZIP/XML 够用。CFB stream 读取的最小新增候选是 `olefile==0.47`。若复用现成 Package 和 PPT 记录解析而不自建解析器，候选组合是 **`olefile==0.47` + `oletools==0.60.2` + 已锁 ZIP/XML 能力**；但它并不覆盖原生 DOC/XLS 子 storage 重建，不能因此承诺“所有 Office 内嵌文档都可恢复”。本调查没有解析新的依赖锁、安装依赖或审计全部传递依赖，不把该组合称为已批准可合并依赖；实施票需要固定整个新依赖闭包并运行样本。

尤其不能直接调用 `oleobj.process_file()` 代替领域提取接口：它遍历 OLE streams，只对末级名称 `\x01Ole10Native` 进行导出；`find_ole()` 在 ZIP 内仅选择 CFB magic 的非 XML part，所以内嵌 XLSX/普通 `Package` stream/原生子 storage 并不会全部被该导出器覆盖。它还直接写文件并打印消息，不提供需要的 occurrence 与状态模型。[oleobj.py 725–805](https://github.com/decalage2/oletools/blob/b565533d6757f3dde2501e946ed6977ebbf2830a/oletools/oleobj.py#L725-L805)、[886–951](https://github.com/decalage2/oletools/blob/b565533d6757f3dde2501e946ed6977ebbf2830a/oletools/oleobj.py#L886-L951)

## 后续最少无模型样本清单

这是一组**尚未执行**的验收用例。可把同组多个对象放进一个小文件；宿主至少覆盖六种扩展名，payload 合计至少覆盖 DOC/DOCX/PPT/PPTX/XLS/XLSX 六种，不必机械构造 36 种组合。已知 payload 先记录长度、SHA-256 和结构，测试只读字节，不运行宏、OLE server、外链或模型。

| 样本组 | 最少内容与要证实的点 |
| --- | --- |
| 1 DOCX | 一个内嵌 package、一个 CFB/Ole10Native 包装；附独立预览图；验证预览不成为附件 |
| 2 PPTX | 一个直接 package 对象、一个原生完整 CFB 对象；验证 slide/shape 与 blob 类型 |
| 3 XLSX | 一个有 shape/anchor 的 OLE 对象；验证 sheet/锚点不是 cell 内容；再有普通本地单元格图表，不能凭空产生子 workbook |
| 4 DOCX 与 PPTX 图表 | 各一个真实内置 XLSX workbook；同一 part 被两处引用；再有外链/仅缓存图表，验证 `chart_data`、多 occurrence 和不访问外链 |
| 5 DOC | ObjectPool 内 Package/Ole10Native 与原生 Word/Excel 多 stream 子 storage 各一项；区分直接提取与重建缺口，不编页码 |
| 6 PPT | 压缩及未压缩 ExOleObjStg；包含内嵌标准 CFB 与包装对象；验证输出长度、magic、内容摘要 |
| 7 PPT 编辑/类别边界 | 有旧 persist 记录或删除对象的文件，加 VBA/ActiveX storage；验证不能将扫描命中全部纳入当前附件 |
| 8 XLS | MBD Package 与原生嵌入 worksheet 各一项；有 sheet/Obj/anchor；验证子 storage 不冒充 XLS，补映射后才报告 cell 区域 |
| 9 OOXML 关系边界 | 同目标跨关系引用、孤立 embedding、悬空 rId、External target、AlternateContent、Strict 命名空间；明确降级状态 |
| 10 受控异常/递归 | 截断 Package、native size 不符、压缩 size 不符、嵌套对象与预算超限、已知私有 ProgID；验证单对象失败不会阻断其他对象 |

保真标准：直接提取/解包装 payload 与嵌入前已知字节匹配；如果 Office 保存时已改写 payload，则对照原件内实际持久化字节，不能声称恢复了嵌入前文件的历史字节。若将来实现 CFB 子树重建，输出容器字节通常不同，应验证必要 streams、结构与下游读取，而不能要求整个容器摘要相同。

## 尚未验证与可精确描述的后续问题

未验证：真实样本的六格式恢复成功率；原生 DOC/XLS 子树重建后的有效性；PPT 活记录/slide/shape 完整映射；DOC CP 与 XLS sheet/anchor 专门解析；Strict/AlternateContent/ChartEx 全覆盖；加密/损坏 Office 的细节；新依赖的全量锁定、许可证闭包及当前 Python/平台兼容性。本报告没有对用户测试库批量解包，也未执行任何 Office、宏或模型。

**可带入已有[《决策：多格式方案达到什么依赖与验收条件才可交付实施？》](https://github.com/wooveep/UrltraKB/issues/72)契约的精确问题：原生标准 Office 子 storage 的重建路径与能力诊断如何满足已约定提取范围？** 这与“私有无法重建对象不支持”不同：有些对象的数据格式已知，只是现最小 reader 组合没有导出能力。现证据支持把它单独标为 `requires_container_rebuild` 并建立专门验收项，不能偷偷归为私有格式，亦不能仅凭 stream 枚举标记成功。它不改变六宿主扫描、独立 Source、图表角色、正文隔离和删除保留等已定政策；无需为此扩张本研究票或立即新增决策票。

依赖选择及二进制位置映射应由后续交付契约根据上述覆盖表具体化；本调查不替用户新增决定、不关闭或编辑 issue/map。研究过程使用 CodeGraph 定位仓库转换路径、agent-reach 的 gh CLI/Jina Reader 读取上游，并以微软规范与固定 tag 源码交叉核验；Exa 未配置时使用内置网页检索。Agent Reach 版本检查结果为 v1.5.0，已是最新。
