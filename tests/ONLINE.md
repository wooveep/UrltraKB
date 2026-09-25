# 在线模型验证入口

真实供应商验证统一调用 `tests.online_model.load_online_model()`。设置唯一的配置知识库：

```bash
export OPENKB_TEST_MODEL_KB=/absolute/path/to/configured-kb
```

也可向函数显式传入 `config_kb`，优先于上述环境变量。配置目录必须存在 `.openkb/config.yaml`；模型和凭据均由该知识库的生产配置解析器获取，保留既有的知识库、进程环境、全局配置优先级。没有配置或凭据时直接报设置错误，不能把 skip 当作在线通过。

```python
from tests.online_model import load_online_model

online = load_online_model()
settings, bundle = online.settings, online.bundle
# 向正常 plan_document/compile_evidence 等入口传入 settings 和 bundle。
# 如直接验证一个请求，也走 _llm_call + processing_scope + compilation_model_options。
```

每个用例事先约定调用数量，使用生产预算限制并保留全部失败。不要在测试里另建供应商客户端、写死模型/端点、读取另一知识库的密钥、增加外围“直到成功”的循环，或修改进程凭据。需要临时更小的试验额度时只能收紧选定配置，不能静默放大用户额度。

`description()` 仅返回允许公开的配置摘要，不包含密钥、额外请求头或完整端点 URL。私有请求可能包含原始文档的敏感内容，不应加入 git 或原样打印。测试结果中区分在线真实调用、受控 HTTP、离线响应回放；后两者不能证明真实模型语义通过。

当前第三步的五个活动研究入口已接入此函数：

- `docs/research/import-review-20260924/step03-reference-recheck/recheck.py`
- `docs/research/import-review-20260924/step03-navigation-reference-validation/recheck.py`
- 同一 navigation-reference-validation 目录的 `short-synthetic.py`、`two-window-synthetic.py`、`review-saved-candidate.py`

真实样例止于第三步的命令：

```bash
OPENKB_TEST_MODEL_KB=/absolute/path/to/configured-kb \
  .venv/bin/python docs/research/import-review-20260924/step03-reference-recheck/recheck.py
```

上述研究入口和样例依赖属于维护者本地资料，不是从干净 checkout 即可运行的公共测试夹具。历史 `run-*`、`recheck-executed.py` 和旧版本诊断脚本保留原样作为执行证据；继续开发时使用活动入口，不重新运行历史脚本作为当前版本验收。

普通 `pytest` 不自动收费调用模型。对可控异常、原子性、权限和恢复使用确定性测试；第三步语义验收以及提示词/协议设计比较必须显式执行在线模型，并报告真实调用数及结果。不得把在线模型答案改成固定期望值后再宣称在线通过。

## 第三步 Markdown 尽力规划验收

先运行 `tests/test_document_markdown_planning.py`、相邻来源/恢复/应用测试和 `tests/test_file_size.py`。旧 `document-plan-v5` 六字段响应、patch 授权、强制引用关系判定和逐块覆盖闭合不再是新规划路径的通过条件。历史计划的读取仍需回归。

固定在线集合为四例。O1 复用保存的真实原文与 PageIndex；O2/O3 是同步、研究两份短资料；O4 显式复用已保存的两窗口输入。活动脚本均使用 `load_online_model()`，只收紧生产预算。各例第三步调用上限依次为 6、6、6、12，包含服务层重试；达到上限即报告，不继续抽样。活动脚本和测试材料属于维护者本地资料，干净 checkout 不包含它们。

```bash
OPENKB_TEST_MODEL_KB=/absolute/path/to/configured-kb .venv/bin/python docs/research/import-review-20260924/step03-navigation-reference-validation/short-synthetic.py --variant=sync
OPENKB_TEST_MODEL_KB=/absolute/path/to/configured-kb .venv/bin/python docs/research/import-review-20260924/step03-navigation-reference-validation/short-synthetic.py --variant=research
OPENKB_TEST_MODEL_KB=/absolute/path/to/configured-kb .venv/bin/python docs/research/import-review-20260924/step03-reference-recheck/recheck.py
OPENKB_TEST_MODEL_KB=/absolute/path/to/configured-kb .venv/bin/python docs/research/import-review-20260924/step03-navigation-reference-validation/two-window-synthetic.py --source-run /absolute/path/to/saved-sync-run
```

记录实际模型、源版本、预算、请求与响应、finish_reason、usage、overview/预览/报告路径、可执行页与遗漏。抽查操作页的 `page_evidence()` 原文、跨章节前提、否定和外部要求；比较 A/B 实际 system 与冻结 W 前缀，确认最终调用参数没有强制 JSON。分别报告流程是否符合合同及内容是否可用；partial 可如实结束，empty 不算内容验收通过。止于第三步的运行要核对 wiki 未变且正文生成、审核、发布均为零。原始正文和响应只存隔离研究目录，不提交 Git。
