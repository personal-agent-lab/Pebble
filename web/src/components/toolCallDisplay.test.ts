import { expect, test } from "vitest";

import type { TimelineItem } from "../api";
import { toolCallDisplay } from "./toolCallDisplay";

type ToolItem = Extract<TimelineItem, { kind: "tool" }>;
const call = (name: string, args: Record<string, unknown> = {}, status: ToolItem["status"] = "ok",
  result: string | null = null): ToolItem => ({
  kind: "tool", item_id: "item", run_id: "run", tool_call_id: "call", name,
  arguments: args, status, result, created_at: "2026-09-24T00:00:00Z",
});

test("所有前台工具和开放的 SDK 内置工具都有可读名称", () => {
  const names = [
    "skill_list", "skill_view", "skill_manage",
    "calendar_list_events", "calendar_get_event", "calendar_check_conflicts", "calendar_create_event",
    "history_search", "history_read", "memory_edit",
    "gmail_search", "gmail_get_thread", "gmail_get_message", "gmail_get_attachment",
    "gmail_prepare_reply", "gmail_prepare_email", "gmail_read_draft", "gmail_update_draft",
    "kb_save", "kb_search", "kb_list", "kb_read", "kb_update", "kb_history",
    "kb_archive", "kb_delete", "kb_move", "kb_restore",
    "WebSearch", "WebFetch", "Read",
  ];
  for (const name of names) {
    const display = toolCallDisplay(call(name));
    expect(display.known, name).toBe(true);
    expect(display.label, name).not.toContain(name);
  }
  expect(toolCallDisplay(call("future_tool"))).toEqual({ label: "future_tool", known: false });
});

test("收起行只取能说明对象的短参数，不把原始字段和正文摆出来", () => {
  expect(toolCallDisplay(call("skill_view", { skill_id: "weekly-report" }, "ok",
    '{"name":"周报整理","body":"很长的技能正文"}'))).toMatchObject({
    label: "已读取技能", target: "周报整理",
  });
  expect(toolCallDisplay(call("calendar_list_events", {
    time_min: "2026-09-21T00:00:00+08:00", time_max: "2026-09-28T00:00:00+08:00",
    max_results: 20,
  }))).toMatchObject({ label: "已查看日历", target: "2026/9/21 00:00 至 2026/9/28 00:00" });
  expect(toolCallDisplay(call("kb_read", { ref: { path: "kb/会议纪要-9月13日.md", lines: [1, 4] } })))
    .toMatchObject({ label: "已读取资料", target: "会议纪要-9月13日" });
  expect(toolCallDisplay(call("WebFetch", { url: "https://docs.qoder.com/cli/sdk?token=private" })))
    .toMatchObject({ label: "已读取网页", target: "docs.qoder.com" });
  expect(toolCallDisplay(call("Read", { file_path: "/tmp/private/agenda.pdf" })))
    .toMatchObject({ label: "已读取附件", target: "agenda.pdf" });
});

test("运行和失败文案与工具状态一致；外部动作不夸大实际结果", () => {
  expect(toolCallDisplay(call("skill_view", { skill_id: "weekly-report" }, "running")))
    .toMatchObject({ label: "正在读取技能" });
  expect(toolCallDisplay(call("history_search", { query: "会议" }, "error")))
    .toMatchObject({ label: "搜索历史消息失败", target: "会议" });
  expect(toolCallDisplay(call("gmail_prepare_email", { subject: "会议安排", body: "正文" })))
    .toMatchObject({ label: "已准备邮件草稿", target: "会议安排" });
  expect(toolCallDisplay(call("calendar_create_event", { summary: "会议" }, "ok",
    '{"status":"conflict"}'))).toMatchObject({ label: "已尝试创建日程", target: "会议" });
});
