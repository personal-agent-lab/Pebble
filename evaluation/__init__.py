"""Pebble 真实场景评测（docs/evaluation.md 的实现载体）。

与 pytest 分工：pytest 用替身锁程序逻辑；本包在真实模型、真实产品接口下
驱动隔离实例验证产品行为。S 层无需外部凭证，M/C/F/L 层需要专用测试账号。
"""
