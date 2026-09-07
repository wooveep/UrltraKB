"""THROWAWAY fixed corpus: semantic expectations are inspected, not assumed from parse success."""

import json

SAMPLES = []


def diagram(key, family, title, source, expected):
    SAMPLES.append(
        {
            "id": key,
            "kind": "mermaid",
            "family": family,
            "title": title,
            "markdown": f"```mermaid\n{source}\n```",
            "expected": expected,
        }
    )


diagram(
    "flow-basic",
    "流程",
    "流程 · 条件与分支",
    "flowchart LR\n  A[Start] --> B{Ready?}\n  B -->|Yes| C[Done]\n  B -->|No| D[Retry]\n  D --> B",
    "Start→Ready；Yes→Done；No→Retry→Ready",
)
diagram(
    "flow-cn",
    "流程",
    "流程 · 中文长标签",
    "flowchart TD\n"
    "  A[导入需要整理的原始资料] --> B{是否已存在于知识库}\n"
    "  B -->|否| C[编译为关联知识页面]\n"
    "  B -->|是| D[跳过重复资料并记录原因]\n"
    "  C --> E[完成后展示结果与检查问题]",
    "中文完整；是→跳过，否→编译；箭头不穿过文字",
)
diagram(
    "sequence-basic",
    "时序",
    "时序 · 请求响应",
    "sequenceDiagram\n"
    "  participant U as User\n"
    "  participant A as App\n"
    "  U->>A: Ask\n"
    "  A-->>U: Answer",
    "User→App 实线 Ask；App→User 虚线 Answer",
)
diagram(
    "sequence-cn",
    "时序",
    "时序 · 中文与循环",
    "sequenceDiagram\n"
    "  actor U as 知识库使用者\n"
    "  participant A as 桌面应用\n"
    "  participant W as 独立工作进程\n"
    "  U->>A: 导入多份中文资料\n"
    "  A->>W: 开始本地任务\n"
    "  loop 每份资料\n"
    "    W-->>A: 返回进度与已保存结果\n"
    "  end\n"
    "  A-->>U: 展示完成情况与失败原因",
    "三个参与者；循环框包住进度；返回方向正确",
)
diagram(
    "class-basic",
    "类",
    "类 · 继承",
    "classDiagram\n"
    "  class Animal {\n"
    "    +String name\n"
    "    +speak()\n"
    "  }\n"
    "  class Cat {\n"
    "    +purr()\n"
    "  }\n"
    "  Animal <|-- Cat",
    "Cat 继承 Animal；空心三角在 Animal 端；属性方法存在",
)
diagram(
    "class-cn",
    "类",
    "类 · 中文关联",
    "classDiagram\n"
    '  class KnowledgeBase["知识库"] {\n'
    "    +String name\n"
    "    +compile()\n"
    "  }\n"
    '  class Document["原始资料"] {\n'
    "    +String title\n"
    "  }\n"
    '  KnowledgeBase "1" --> "many" Document : 管理和编译',
    "知识库/原始资料标题完整；1/many 与管理和编译在正确位置",
)
diagram(
    "state-basic",
    "状态",
    "状态 · 分支回环",
    "stateDiagram-v2\n"
    "  [*] --> Idle\n"
    "  Idle --> Running: start\n"
    "  Running --> Idle: stop\n"
    "  Running --> [*]: done",
    "起点到 Idle；start进入 Running；stop返回 Idle；done到终点",
)
diagram(
    "state-cn",
    "状态",
    "状态 · 中文状态",
    "stateDiagram-v2\n"
    '  state "等待知识库可写" as Waiting\n'
    '  state "正在编译资料" as Running\n'
    '  state "完成并保存结果" as Finished\n'
    "  [*] --> Waiting\n"
    "  Waiting --> Running: 获得执行资格\n"
    "  Running --> Finished: 安全提交\n"
    "  Finished --> [*]",
    "三个中文状态及两条边标签完整；路径按顺序到终点",
)
diagram(
    "er-basic",
    "ER",
    "ER · 基数关系",
    "erDiagram\n"
    "  CUSTOMER ||--o{ ORDER : places\n"
    "  CUSTOMER {\n"
    "    int id PK\n"
    "    string name\n"
    "  }\n"
    "  ORDER {\n"
    "    int id PK\n"
    "    int customer_id FK\n"
    "  }",
    "CUSTOMER一端，ORDER零到多；PK/FK可辨认",
)
diagram(
    "er-cn",
    "ER",
    "ER · 中文属性说明",
    "erDiagram\n"
    '  KNOWLEDGE_BASE ||--o{ DOCUMENT : "管理原始资料"\n'
    "  KNOWLEDGE_BASE {\n"
    '    int id PK "知识库编号"\n'
    '    string title "知识库名称"\n'
    "  }\n"
    "  DOCUMENT {\n"
    '    int id PK "资料编号"\n'
    '    string title "完整中文资料标题"\n'
    "  }",
    "关系中文与两张表中的属性说明不被截断；零到多基数正确",
)
diagram(
    "mindmap-basic",
    "思维导图",
    "思维导图 · 层级",
    "mindmap\n  root((Knowledge))\n    Sources\n      Files\n      URLs\n    Wiki\n    Chat",
    "root含Sources/Wiki/Chat；Files/URLs只属于Sources",
)
diagram(
    "mindmap-cn",
    "思维导图",
    "思维导图 · 中文",
    "mindmap\n"
    "  root((个人知识库))\n"
    "    原始资料\n"
    "      本地文件\n"
    "      网络文章\n"
    "    编译后的知识\n"
    "      概念页面\n"
    "      实体关联\n"
    "    对话与生成产物",
    "三主分支与四子分支；长中文不碰撞",
)
diagram(
    "pie-basic",
    "饼图",
    "饼图 · 数值比例",
    'pie showData\n  title Document status\n  "Added" : 6\n  "Skipped" : 3\n  "Failed" : 1',
    "60%/30%/10% 对应Added/Skipped/Failed",
)
diagram(
    "pie-cn",
    "饼图",
    "饼图 · 中文图例",
    """pie showData
  title 本次资料导入结果
  "成功编译并保存" : 6
  "跳过重复资料" : 3
  "处理失败待检查" : 1""",
    "三份中文图例完整，数值与扇区对应",
)
diagram(
    "gantt-basic",
    "Gantt",
    "Gantt · 日期与依赖",
    "gantt\n"
    "  title Prototype\n"
    "  dateFormat YYYY-MM-DD\n"
    "  axisFormat %m-%d\n"
    "  todayMarker off\n"
    "  section Rendering\n"
    "  Formula :a, 2026-09-01, 3d\n"
    "  Diagram :b, after a, 4d\n"
    "  Review :milestone, after b, 0d",
    "Formula起于09-01；Diagram在Formula之后；Review里程碑",
)
diagram(
    "gantt-cn",
    "Gantt",
    "Gantt · 中文日程",
    "gantt\n"
    "  title 原生渲染验证计划\n"
    "  dateFormat YYYY-MM-DD\n"
    "  axisFormat %m-%d\n"
    "  todayMarker off\n"
    "  section 本地验证\n"
    "  公式与中文样本 :a, 2026-09-01, 3d\n"
    "  九类图表显示 :b, after a, 4d\n"
    "  section 跨平台复验\n"
    "  Windows 样本复验 :c, after b, 3d",
    "中文章节与任务完整；先公式再图表再Windows",
)
diagram(
    "c4-basic",
    "C4",
    "C4 · 系统关系",
    "C4Context\n"
    "  title Knowledge workbench\n"
    '  Person(user, "User", "Knowledge worker")\n'
    '  System(app, "Desktop app", "Local knowledge")\n'
    '  System_Ext(model, "Model service", "LLM provider")\n'
    '  Rel(user, app, "Uses")\n'
    '  Rel(app, model, "Requests", "HTTPS")',
    "User→Desktop→Model；Uses/Requests/HTTPS文字正确",
)
diagram(
    "c4-cn",
    "C4",
    "C4 · 中文系统描述",
    "C4Context\n"
    "  title 本地知识库桌面应用\n"
    '  Person(user, "知识库使用者", "整理资料并进行问答")\n'
    '  System(app, "原生桌面工作台", "在本机管理多个知识库")\n'
    '  System_Ext(model, "模型服务", "提供编译与问答能力")\n'
    '  Rel(user, app, "导入、阅读与提问")\n'
    '  Rel(app, model, "发送模型请求", "HTTPS")',
    "三个角色的中文名称/描述完整；关系与方向正确",
)

FORMULAS = [
    ("fraction", "分数与上下标", r"\frac{a_1+b^2}{c_3}", "分子a₁+b²，分母c₃"),
    ("root", "根式与嵌套", r"\sqrt{x^2+y^2}+\sqrt[3]{\frac{a}{b}}", "二次根和三次根指数、分数位置"),
    (
        "integral",
        "积分与极限",
        r"\lim_{n\to\infty}\sum_{k=1}^{n}\frac1{k^2}=\int_0^1 f(x)\,dx",
        "极限与求和上下限、积分上下限",
    ),
    (
        "matrix",
        "矩阵",
        r"A=\begin{bmatrix}1&2&3\\4&5&6\end{bmatrix}",
        "2行3列，左右方括号包住整矩阵",
    ),
    (
        "cases",
        "分段函数",
        r"f(x)=\begin{cases}x^2,&x\ge0\\-x,&x<0\end{cases}",
        "大左括号；两行条件位置对应",
    ),
    ("aligned", "多行对齐", r"\begin{aligned}a&=b+c\\&=d+e+f\end{aligned}", "两行等号对齐"),
    (
        "chinese",
        "中文说明",
        r"\text{输入资料数量}=n,\quad\text{成功数量}=n-k",
        "中文与数学混排不碰撞、不缺字",
    ),
    (
        "chinese-frac",
        "中文分数",
        r"\text{成功率}=\frac{\text{成功编译的资料数量}}{\text{全部资料数量}}\times100\%",
        "分子分母长中文完整，分数线覆盖文字宽度",
    ),
    (
        "accents",
        "向量与重音",
        r"\vec{v}=\hat{x}+\bar{y},\quad\overline{AB}\perp\overrightarrow{CD}",
        "向量箭头、帽、横线位置正确",
    ),
    (
        "delimiters",
        "伸缩括号",
        r"\left(\frac{1}{1+\frac1x}\right)^2+\left\lVert A\right\rVert",
        "嵌套分数括号与范数双竖线",
    ),
    ("inline", "行内公式基线", r"\frac{a_i}{b_j}+x^2", "公式置于中文正文中，分母低于正文基线"),
    (
        "macro",
        "局部宏",
        r"\newcommand{\vect}[1]{\mathbf{#1}}\vect{x}+\vect{y}",
        "局部宏展开为粗体x和y",
    ),
]
for key, title, source, expected in FORMULAS:
    display = key != "inline"
    markup = f"\\[\n{source}\n\\]" if display else f"\\({source}\\)"
    SAMPLES.append(
        {
            "id": "math-" + key,
            "kind": "math",
            "family": "公式",
            "title": title,
            "markdown": markup,
            "display": display,
            "expected": expected,
        }
    )
SAMPLES.extend(
    [
        {
            "id": "error-macro",
            "kind": "math",
            "family": "错误",
            "title": "未知宏必须报错",
            "markdown": r"\[\notARealMacro{x}\]",
            "expected_error": True,
            "expected": "明确错误，不能显示红字就算渲染成功",
        },
        {
            "id": "error-diagram",
            "kind": "mermaid",
            "family": "错误",
            "title": "无效图表必须报错",
            "markdown": "```mermaid\nthisIsNotADiagram\n```",
            "expected_error": True,
            "expected": "明确无法解析，原文保留",
        },
    ]
)

if __name__ == "__main__":
    print(json.dumps(SAMPLES, ensure_ascii=False, indent=2))
