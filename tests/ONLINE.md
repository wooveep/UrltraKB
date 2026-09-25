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

## 第三步 JSON 响应与关系核对验收

确定性回归先运行 `tests/test_document_json_recovery.py`、`tests/test_document_json_prompts.py`、`tests/test_document_reference_check.py` 和相邻规划、修复、恢复测试。随后固定选择三份输入，各运行一次：

```bash
OPENKB_TEST_MODEL_KB=/absolute/path/to/configured-kb .venv/bin/python docs/research/import-review-20260924/step03-navigation-reference-validation/short-synthetic.py --variant=sync
OPENKB_TEST_MODEL_KB=/absolute/path/to/configured-kb .venv/bin/python docs/research/import-review-20260924/step03-navigation-reference-validation/short-synthetic.py --variant=research
OPENKB_TEST_MODEL_KB=/absolute/path/to/configured-kb .venv/bin/python docs/research/import-review-20260924/step03-reference-recheck/recheck.py
```

前两份短资料验证不同领域的计划与关系核对；最后一份复用第二步真实产物，并止于第三步。保存每次运行的目录和完整失败证据。若一次运行失败，记录失败及 reference_check 是否到达，不增加同输入采样次数来刷通过。只有计划、范围授权、必要引用和业务门禁都完成，才能报告第三步完整验收通过。
