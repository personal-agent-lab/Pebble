// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { type TimelineItem } from "../api";
import CalendarPreviewCard from "./CalendarPreviewCard";

const { editCalendarPreview, confirmOperation, verifyExecution } = vi.hoisted(() => ({
  editCalendarPreview: vi.fn(), confirmOperation: vi.fn(), verifyExecution: vi.fn(),
}));
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, editCalendarPreview, confirmOperation, verifyExecution };
});

const item: Extract<TimelineItem, { kind: "calendar_preview" }> = {
  item_id: "card-1", kind: "calendar_preview", run_id: "run-1", operation_id: "op-1",
  created_at: "2026-09-14T00:00:00Z",
  preview: { operation_id: "op-1", version: 2, status: "pending", calendar_id: "primary",
    summary: "项目会议", start: "2026-10-12T10:00:00+08:00",
    end: "2026-10-12T11:00:00+08:00", all_day: false, location: "会议室", description: "讨论" },
  execution: { operation_id: "op-1", version: 2, status: "pending", confirmation: null, result: null },
};

beforeEach(() => {
  editCalendarPreview.mockReset(); confirmOperation.mockReset(); verifyExecution.mockReset();
  editCalendarPreview.mockResolvedValue({ operation_id: "op-1", version: 3, status: "pending" });
  confirmOperation.mockResolvedValue(item.execution);
});
afterEach(cleanup);

test("修改后必须先保存，确认只绑定保存版本", async () => {
  render(<CalendarPreviewCard taskId="task-1" item={item} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  await userEvent.clear(screen.getByLabelText("标题"));
  await userEvent.type(screen.getByLabelText("标题"), "最终会议");
  expect((screen.getByRole("button", { name: "确认创建" }) as HTMLButtonElement).disabled).toBe(true);
  await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
  await waitFor(() => expect(editCalendarPreview).toHaveBeenCalledWith("op-1", 2, expect.objectContaining({ summary: "最终会议" })));
});

test("定向修改和待核实都不触发再次创建", async () => {
  const sendMessage = vi.fn().mockResolvedValue(null);
  const { rerender } = render(<CalendarPreviewCard taskId="task-1" item={item} sendMessage={sendMessage} onChanged={vi.fn()} />);
  await userEvent.type(screen.getByPlaceholderText("告诉 Agent 怎样修改这项日程"), "改到下午");
  await userEvent.click(screen.getByRole("button", { name: "请 Agent 修改" }));
  expect(sendMessage).toHaveBeenCalledWith("改到下午", { kind: "calendar_preview", operation_id: "op-1" });
  rerender(<CalendarPreviewCard taskId="task-1" item={{ ...item, execution: { ...item.execution,
    status: "unknown", result: { status: "unknown", reason: "网络中断" } } }} sendMessage={sendMessage} onChanged={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "核实结果" }));
  expect(verifyExecution).toHaveBeenCalledWith("op-1");
  expect(confirmOperation).not.toHaveBeenCalled();
});
