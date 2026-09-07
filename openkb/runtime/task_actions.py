"""Human-requested retry plans reconcile facts without replaying whole batches."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from openkb.application.knowledge_bases import get_kb_list
from openkb.locks import kb_read_lock
from openkb.runtime.records import read_receipt
from openkb.runtime.tasks import TaskManager


@dataclass(frozen=True)
class RetryPreview:
    task_id: str
    kb_dir: str
    operation: str
    version: str
    indices: tuple[int, ...]
    retained: tuple[str, ...]
    missing: tuple[str, ...]
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.reason is None and bool(self.indices)


def preview_retry(manager: TaskManager, task_id: str) -> RetryPreview:
    view, requests, identities = manager.retry_inputs(task_id)
    root = Path(view.kb_dir)
    if not (root / ".openkb/config.yaml").is_file() or not (root / "wiki").is_dir():
        raise ValueError("任务所属知识库已移动或不可用，请重新打开知识库")
    with kb_read_lock(root / ".openkb"):
        get_kb_list(root)  # Shared recovery barrier and registry shape validation.
        results = []
        for identity in identities:
            result = read_receipt(manager.receipt_dir, identity)
            if result is None:
                break
            results.append(result)
        reason = None
        if len(results) < len(view.results):
            reason = "结果凭据缺失或损坏。请先检查任务成果和诊断，不能据此重复执行。"
        elif view.state == "interrupted" and len(results) < view.total:
            reason = "上次执行仍有未能确认的成果。请检查产物后从相应操作界面重新发起。"
        elif not requests:
            reason = "历史只保存结果摘要。请从相应操作界面重新输入请求并发起新任务。"
        indices = tuple(
            index
            for index in range(view.total)
            if index >= len(results) or results[index].status in {"failed", "stopped", "blocked"}
        )
        if not indices and reason is None:
            reason = "所有执行单元已有完成或跳过结果。可从相应操作界面发起新的请求。"
        references = tuple(dict.fromkeys(path for result in results for path in result.resources))
        retained = tuple(path for path in references if Path(path).exists())
        missing = tuple(path for path in references if path not in retained)
        version = hashlib.sha256(
            json.dumps(
                {
                    "view": view.summary(),
                    "receipts": [result.summary() for result in results],
                    "retained": retained,
                    "missing": missing,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return RetryPreview(
            task_id, str(root), view.operation, version, indices, retained, missing, reason
        )


def retry_task(manager: TaskManager, preview: RetryPreview) -> str:
    """Create a new task after rechecking the reviewed outcomes and KB health."""
    current = preview_retry(manager, preview.task_id)
    if current.version != preview.version or current.indices != preview.indices:
        raise ValueError("任务成果已变化，请重新查看重试预览")
    if not current.allowed:
        raise ValueError(current.reason)
    _view, requests, _identities = manager.retry_inputs(preview.task_id)
    # submit creates new identities and a fresh, initially absent snapshot.
    # Original overwrite/CAS conditions stay attached to each retained request.
    return manager.submit(
        Path(current.kb_dir),
        [requests[index] for index in current.indices],
        retry_of=preview.task_id,
    )
