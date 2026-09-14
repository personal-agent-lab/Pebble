// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApiError, type TimelineItem } from "../api";
import MailDraftCard from "./MailDraftCard";

const { editDraft, confirmOperation, verifyExecution } = vi.hoisted(() => ({
  editDraft: vi.fn(), confirmOperation: vi.fn(), verifyExecution: vi.fn(),
}));
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, editDraft, confirmOperation, verifyExecution };
});

const item: Extract<TimelineItem, { kind: "mail_draft" }> = {
  item_id: "card-1", kind: "mail_draft", run_id: "run-1", operation_id: "op-1", created_at: "2026-09-14T00:00:00Z",
  draft: {
    operation_id: "op-1", kind: "new", version: 3, status: "pending",
    to: ["office@example.edu"], subject: "证明文件", body: "完整正文",
  },
  execution: { operation_id: "op-1", version: 3, status: "pending", confirmation: null, result: null },
};

beforeEach(() => {
  editDraft.mockReset(); confirmOperation.mockReset(); verifyExecution.mockReset();
  editDraft.mockResolvedValue({ version: 4 });
  confirmOperation.mockResolvedValue(item.execution);
});
afterEach(cleanup);

test("修改要求按钮就地展开输入，发送绑定 operation_id 的新一轮对话", async () => {
  const sendMessage = vi.fn().mockResolvedValue(null);
  render(<MailDraftCard taskId="task-1" item={item} sendMessage={sendMessage} onChanged={vi.fn()} />);
  expect(screen.queryByLabelText("针对这封邮件提出修改要求")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "修改要求" }));
  await userEvent.type(screen.getByLabelText("针对这封邮件提出修改要求"), "语气更正式{enter}");
  expect(sendMessage).toHaveBeenCalledWith("语气更正式", { kind: "mail_draft", operation_id: "op-1" });
  await waitFor(() => expect(screen.queryByLabelText("针对这封邮件提出修改要求")).toBeNull());
});

test("收件人折叠成一行，点击后就地变成输入", async () => {
  render(<MailDraftCard taskId="task-1" item={item} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.queryByLabelText("收件人")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: /收件人/ }));
  expect((screen.getByLabelText("收件人") as HTMLInputElement).value).toBe("office@example.edu");
});

test("正文常驻可编辑，失焦即按当前版本保存", async () => {
  const onChanged = vi.fn().mockResolvedValue(undefined);
  render(<MailDraftCard taskId="task-1" item={item} sendMessage={vi.fn()} onChanged={onChanged} />);
  const body = screen.getByLabelText("正文");
  await userEvent.clear(body);
  await userEvent.type(body, "改过的正文");
  expect(editDraft).not.toHaveBeenCalled();
  await userEvent.tab();
  await waitFor(() => expect(editDraft).toHaveBeenCalledWith("op-1", 3, {
    to: ["office@example.edu"], subject: "证明文件", body: "改过的正文",
  }));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

test("失焦保存撞上版本冲突时保留本地内容并提示", async () => {
  editDraft.mockRejectedValue(new ApiError("version_conflict", "当前版本为 4", 409, 4));
  render(<MailDraftCard taskId="task-1" item={item} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  const subject = screen.getByLabelText("主题");
  await userEvent.clear(subject);
  await userEvent.type(subject, "更正式的主题");
  await userEvent.tab();
  expect(await screen.findByText("草稿已被更新，你的修改尚未保存")).toBeTruthy();
  expect((screen.getByLabelText("主题") as HTMLInputElement).value).toBe("更正式的主题");
});

test("重新载入最新内容会丢弃未保存的本地值", async () => {
  const onChanged = vi.fn().mockResolvedValue(undefined);
  editDraft.mockRejectedValue(new ApiError("version_conflict", "当前版本为 4", 409, 4));
  render(<MailDraftCard taskId="task-1" item={item} sendMessage={vi.fn()} onChanged={onChanged} />);
  await userEvent.clear(screen.getByLabelText("主题"));
  await userEvent.type(screen.getByLabelText("主题"), "尚未保存");
  await userEvent.tab();
  await userEvent.click(await screen.findByRole("button", { name: "重新载入最新内容" }));
  expect((screen.getByLabelText("主题") as HTMLInputElement).value).toBe("证明文件");
  expect(onChanged).toHaveBeenCalled();
});

test("确认发送先落盘未保存的改动，再按新版本确认", async () => {
  render(<MailDraftCard taskId="task-1" item={item} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  await userEvent.clear(screen.getByLabelText("主题"));
  await userEvent.type(screen.getByLabelText("主题"), "最终主题");
  await userEvent.click(screen.getByRole("button", { name: /确认并发送/ }));
  await waitFor(() => expect(confirmOperation).toHaveBeenCalledWith("task-1", "op-1", 4));
  expect(editDraft).toHaveBeenCalledOnce();
});

test("没有收件人时不能确认发送", () => {
  render(<MailDraftCard taskId="task-1" item={{ ...item, draft: { ...item.draft, to: [] } }}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect((screen.getByRole("button", { name: /确认并发送/ }) as HTMLButtonElement).disabled).toBe(true);
});

test("发送中、成功、失败和待核实都在原卡片呈现", () => {
  const confirmation = { task_id: "task-1", version: 3, confirmed_at: "2026-09-14T00:00:00Z" };
  const { rerender } = render(<MailDraftCard taskId="task-1" item={{
    ...item, execution: { ...item.execution, status: "sending", confirmation },
  }} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.getByText("已确认，正在发送。内容已经锁定。")).toBeTruthy();
  expect((screen.getByLabelText("正文") as HTMLTextAreaElement).disabled).toBe(true);
  expect(screen.queryByRole("button", { name: /确认并发送/ })).toBeNull();

  rerender(<MailDraftCard taskId="task-1" item={{
    ...item, execution: { ...item.execution, status: "sent", confirmation,
      result: { status: "sent", message_id: "gmail-1" } },
  }} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.getByText("已发送至 office@example.edu。")).toBeTruthy();

  rerender(<MailDraftCard taskId="task-1" item={{
    ...item, execution: { ...item.execution, status: "failed", confirmation,
      result: { status: "failed", reason: "被拒绝" } },
  }} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.getByText("发送失败：被拒绝。系统不会自动重试。")).toBeTruthy();

  rerender(<MailDraftCard taskId="task-1" item={{
    ...item, execution: { ...item.execution, status: "unknown", confirmation,
      result: { status: "unknown", reason: "网络中断" } },
  }} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.getByText("结果待核实：网络中断。未核实前不能再次发送。")).toBeTruthy();
  expect(screen.getByRole("button", { name: "核实实际结果" })).toBeTruthy();
});
