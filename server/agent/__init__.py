"""Agent 装配层：把 Qoder Agent SDK 接进 Pebble。

- `toolset.py`：把注册表里的工具绑定到依赖，并按轮次划定模型可见范围；
- `prompt.py`：不随轮次变化的基础人设与约束；
- `context.py`：组装每轮系统提示与技能名单；
- `client.py`：`AgentGateway` 的 SDK 实现。

本层只做装配，不实现自有 Agent 循环，也不调用外部写操作。
"""
