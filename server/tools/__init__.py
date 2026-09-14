"""Pebble 工具层：集中注册与按服务分目录实现。

注册表是装配的唯一来源。工具模块在被导入时完成注册，这里导入各服务的工具模块；
接入新服务时加一行导入即可。
"""

from server.tools.calendar import tools as calendar_tools
from server.tools.gmail import tools as gmail_tools

__all__ = ["calendar_tools", "gmail_tools"]
