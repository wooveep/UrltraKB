# Keep source identity separate from reusable analysis

Every parsed source has a complete, immutable range map before compilation; optional
model-generated structure and summaries enrich that map within a bounded allowance.
Query and chat capture the published source, version, parse and index together, so a
rebuild cannot silently change the evidence behind an answer. Native pages, paragraphs,
cells and slide objects retain their own position types instead of becoming artificial
PDF pages.

The release stores indexes exclusively in `.openkb/pageindex.db`, through the pinned
PageIndex `LocalClient.collection().add()` and `SQLiteStorage`. A native parser adapter
supplies validated structure; standard SDK document rows hold trees and original block
text. The `openkb_source_indexes` table in that same database binds immutable source,
version, parse and index identities, and supports prepared-index lookup. Its metadata
never duplicates the tree or original text. Compiler and question tools use collection
structure and page-content reads, then restore native coordinates from the bound parse.
SDK range reads are batched at its 1,000-range limit. There is no JSON index compatibility,
migration or missing-index fallback. Damaged published data fails validation; explicit
processing can replace it with a new immutable database generation.

The SDK deduplicates input bytes without considering parsing options. A small managed
input descriptor identifies each generation, avoiding accidental reuse of a damaged or
differently parsed row. It contains identities only; the tree and original text are in
the database. The SDK's managed files are append-only, permitting hardlinked mutation
backups. Recovery covers its copy-before-parse window, the database and binding insert.
Usage receipts and immutable original/parse assets retain their existing source-store
roles; they are not alternate index stores.

Original-range tools also expose exact published image destinations, bound to the
block's asset identity and checked against the published bytes. Missing, damaged or
symbolically linked images are unavailable without preventing a text answer. Answer
generation consumes those destinations directly instead of constructing paths from
document names.

Identical analysis can be reused inside a knowledge base, while each adopting source
retains a separate binding and publication transaction. The shared identity includes the
entire semantic request, ordered context, model options and validating rules. A successful
dispatch carries its actual output cap; concurrent adaptive expansion cannot relabel an
earlier response. A stable coordination identity covers the complete adaptive attempt,
including retries. Shared drafts never grant publication permission, and generation and
verification have independent reuse contracts.

Completed facts for the same immutable source may resume after batch budgets change when
their complete extraction contract and original quotations still validate. Fact context
retains node meaning and original ranges, without the index artifact's execution-budget
identity. Valid empty results remain recoverable for their own source and complete batch;
they do not become evidence for another source. Other local
stages retain their configured request policy, including previously successful adaptive
expansion. A new shared consumer matches the actual expected request cap. Legacy records
without sufficient provenance can supply only explicitly supported recovery inputs,
followed by current validation. These
boundaries cost metadata and conservative cache misses, but keep position movement,
source deletion, process interruption and rule changes from silently changing evidence.

Dependency review keeps complete operation and omission scopes, with ancestor conditions
and explicit reference targets. Structure locates evidence; a missing heading or keyword
match never proves independence. Every candidate receives a semantic decision against the
known omitted scopes. Unlocated conditions expand to the whole original; if that required
input alone cannot fit, capacity preflight records a content omission before generation.
Generated ownership annotations become ordered structured segments, and repeated full
references use a reversible dictionary. Batches contain at most three candidates, each
with an independent decision. Newly withdrawn candidates propagate as new omissions until
the remaining decisions are stable. Receipts bind the actual original scopes, candidate
bytes, omissions, verification options and message rules; an unknown result stays unknown.

Private compilation bodies and detached source ranges are disposable, bounded working
storage, not another source index. Completed facts and candidates retain their existing
durable checkpoints, and only the Wiki mutation transaction grants publication permission.
Request identity computation does not allocate a temporary contract. Active contracts use
reference-counted ownership; explicit close releases their private store. Crash collection
uses the existing process/owner lease and never deletes a live owner or persistent result.
The bounded task scheduler serializes work in one KB, including reparses and attachments;
its task family shares cumulative request, token and model-time reservations across retries.

Removing a source withdraws its explicitly delimited contribution inside the same Wiki
mutation transaction. Both the current publication baseline and the source’s own completed
proposal must authorize ownership; accepting another source’s link cleanup cannot transfer
authorship of retained manual text or authorize a later update to overwrite it. Editable
summary metadata cannot override the source's immutable prior publication. Generated summaries receive boundaries only after all
notices are rendered. Surrounding text, custom metadata and unclassified legacy summaries
remain intact. Modified, crossed or nested ownership ranges cannot authorize removal. History cleanup follows
current and historical citations plus surviving analysis bindings; the producer identity
alone does not keep an otherwise unreferenced original alive.

依赖检查仅将通过完整候选身份及覆盖协议的返回保存为可复用语义结论。无效返回按响应身份保存原文与诊断，继续可有界重试。普通内容无法容纳时按已接受的内容遗漏政策排除；请求、tokens、时间硬额度、身份或保存故障仍使任务未完成。有效否定和未知结论保留理由，不能通过继续反复抽样。已明确列出的历史事实合同只有在原文、完整上下文、模型选项、输出校验均仍匹配时才能恢复，不进行宽泛的旧缓存迁移。
