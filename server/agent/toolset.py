"""按装配依赖绑定业务工具，供 SDK 装配使用。

工具实现声明仅关键字的依赖参数（Gmail 客户端、草稿存储、任务存储），这里用闭包把装配期
构造的实例绑定进去。进程内没有工具依赖的全局单例：同一进程可以装配多套互不影响的应用，
测试也不需要改写模块私有状态。

未接入 Gmail 时绑定 `UnavailableGmailClient`：工具清单与 schema 不随装配变化，调用时按
"依赖未接入"拒绝，不退回模拟邮箱。
"""

from dataclasses import replace
from functools import partial
from inspect import Parameter, signature

from server.errors import DependencyUnavailableError
from server.sessions.service import SessionStore
from server.tools.gmail import tools as gmail_tools  # noqa: F401  按 import 副作用完成注册
from server.tools.gmail.client import BaseGmailClient, GmailMessage, SendReplyResult
from server.tools.gmail.service import ReplyDraftStore
from server.tools.registry import ToolDefinition, default_registry


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
    """返回绑定好依赖的工具定义；名称、说明、参数与副作用声明保持注册时的原样。

    工具清单来自注册表，不是手写列表：新注册的工具自动进入装配，声明了依赖却没法绑定时
    在装配期就失败，不会等到模型调用才发现。
    """
    available = {
        "client": gmail if gmail is not None else UnavailableGmailClient(),
        "drafts": drafts,
        "tasks": tasks,
    }
    bound = []
    for definition in default_registry.list_tools():
        names = [
            name
            for name, parameter in signature(definition.func).parameters.items()
            if parameter.kind is Parameter.KEYWORD_ONLY
        ]
        missing = [name for name in names if name not in available]
        if missing:
            raise RuntimeError(f"工具 {definition.name} 声明了无法装配的依赖：{missing}")
        dependencies = {name: available[name] for name in names}
        bound.append(replace(definition, func=partial(definition.func, **dependencies)))
    return bound
