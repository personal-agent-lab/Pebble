"""Gmail 草稿持久化接口协议与内存桩。

遵循 docs/v1-mail-flow-contract.md §4 约定：
- 解耦 A 的 SQLite 会话持久化；
- 在 A 尚未合入数据库前，提供 InMemoryDraftStorage 供 B 独立测试与运行；
- 遵循幂等去重规则：同一 source_message_id 复用已有 operation_id，不重复创建。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict


class DraftSaveResult(TypedDict):
    operation_id: str
    version: int
    status: str


class DraftStorageProtocol(Protocol):
    """A 提供的草稿持久化存储协议。"""

    def save_reply_draft(
        self,
        task_id: str,
        source_message_id: str,
        thread_id: str,
        to: list[str],
        subject: str,
        body: str,
    ) -> DraftSaveResult:
        """保存回复草稿，并管理版本与根据 source_message_id 去重。"""
        ...


@dataclass
class InMemoryDraftStorage(DraftStorageProtocol):
    """用于测试与解耦开发期的内存草稿存储桩。"""

    # operation_id -> draft_data
    drafts: dict[str, dict[str, Any]] = field(default_factory=dict)
    # source_message_id -> operation_id 索引（用于业务去重）
    source_to_op: dict[str, str] = field(default_factory=dict)

    def save_reply_draft(
        self,
        task_id: str,
        source_message_id: str,
        thread_id: str,
        to: list[str],
        subject: str,
        body: str,
    ) -> DraftSaveResult:
        # 重复准备去重检查：若原邮件已有回复草稿操作，直接复用
        if source_message_id in self.source_to_op:
            existing_op_id = self.source_to_op[source_message_id]
            existing_draft = self.drafts[existing_op_id]
            return {
                "operation_id": existing_op_id,
                "version": existing_draft["version"],
                "status": existing_draft["status"],
            }

        # 首次保存：分配 operation_id，初始版本为 1，状态为 pending
        op_id = f"op_{uuid.uuid4().hex[:8]}"
        new_draft = {
            "operation_id": op_id,
            "task_id": task_id,
            "source_message_id": source_message_id,
            "thread_id": thread_id,
            "to": to,
            "subject": subject,
            "body": body,
            "version": 1,
            "status": "pending",
        }

        self.drafts[op_id] = new_draft
        self.source_to_op[source_message_id] = op_id

        return {
            "operation_id": op_id,
            "version": 1,
            "status": "pending",
        }

    def get_draft(self, operation_id: str) -> dict[str, Any] | None:
        return self.drafts.get(operation_id)

    def clear(self) -> None:
        self.drafts.clear()
        self.source_to_op.clear()


# 全局默认存储实例（后续由 A 的真实 SQLite 实现注入替换）
_default_storage: DraftStorageProtocol = InMemoryDraftStorage()


def get_draft_storage() -> DraftStorageProtocol:
    """获取当前配置的草稿存储提供者。"""
    return _default_storage


def set_draft_storage(storage: DraftStorageProtocol) -> None:
    """设置草稿存储提供者（供 A 的 SQLite 模块接入或测试注入）。"""
    global _default_storage
    _default_storage = storage
