"""新邮件来源的装配插孔：应用启动时开始检测，关闭时停止。

真实检测由 B 接入：Gmail 同步游标与协议处理属于 Gmail 工具，检测到未处理邮件后调用
`GatewayRuntime.accept_new_mail`，去重与任务创建仍由 A 保证。本模块只定义启停接口，
不含检测逻辑；默认装配没有邮件来源，不伪造邮件。
"""

from typing import Protocol

from server.gateway.runtime import GatewayRuntime


class MailSource(Protocol):
    async def start(self, agent: GatewayRuntime) -> None:
        """在应用事件循环中开始检测；实现自行持有后台任务。"""
        ...

    async def stop(self) -> None:
        """停止检测并等待后台任务结束。"""
        ...
