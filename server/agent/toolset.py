"""按装配依赖绑定业务工具，供 SDK 装配使用。

工具实现声明仅关键字的依赖参数（Gmail 客户端、草稿存储、任务存储），这里用闭包把装配期
构造的实例绑定进去。进程内没有工具依赖的全局单例：同一进程可以装配多套互不影响的应用，
测试也不需要改写模块私有状态。

未接入 Gmail 时绑定 `UnavailableGmailClient`：工具清单与 schema 不随装配变化，调用时按
"依赖未接入"拒绝，不退回模拟邮箱。
"""

from dataclasses import replace
from functools import partial

from server.errors import DependencyUnavailableError
from server.sessions.service import SessionStore
from server.tools.gmail import tools as gmail_tools
from server.tools.gmail.client import BaseGmailClient, GmailMessage, SendReplyResult
from server.tools.gmail.service import ReplyDraftStore
from server.tools.registry import ToolDefinition


class UnavailableGmailClient(BaseGmailClient):
    """Gmail 未接入时的占位客户端：任何调用都拒绝，不伪造邮件内容。"""

    def _unavailable(self) -> DependencyUnavailableError:
        return DependencyUnavailableError("Gmail 尚未接入")

    def send_raw_message(self, raw: str, thread_id: str) -> SendReplyResult:
        raise self._unavailable()

    def get_message(self, message_id: str) -> GmailMessage:
        raise self._unavailable()

    def get_thread(self, thread_id: str) -> list[GmailMessage]:
        raise self._unavailable()

    def search_messages(self, query: str, max_results: int = 10) -> list[dict[str, str]]:
        raise self._unavailable()


def build_tools(
    *,
    drafts: ReplyDraftStore,
    tasks: SessionStore,
    gmail: BaseGmailClient | None = None,
) -> list[ToolDefinition]:
    """返回绑定好依赖的工具定义；名称、说明、参数与副作用声明保持注册时的原样。"""
    client = gmail if gmail is not None else UnavailableGmailClient()
    bindings: list[tuple[ToolDefinition, dict]] = [
        (gmail_tools.query_emails, {"client": client}),
        (gmail_tools.get_email_thread, {"client": client}),
        (gmail_tools.get_email_detail, {"client": client}),
        (gmail_tools.prepare_reply, {"drafts": drafts}),
        (gmail_tools.read_reply_draft, {"drafts": drafts, "tasks": tasks}),
        (gmail_tools.update_reply_draft, {"drafts": drafts, "tasks": tasks}),
    ]
    return [
        replace(definition, func=partial(definition.func, **dependencies))
        for definition, dependencies in bindings
    ]
