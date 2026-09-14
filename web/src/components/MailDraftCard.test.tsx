// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApiError, type TimelineItem } from "../api";
import MailDraftCard from "./MailDraftCard";

const { editDraft } = vi.hoisted(() => ({ editDraft: vi.fn() }));
vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, editDraft, confirmOperation: vi.fn(), verifyExecution: vi.fn(), uploadFile: vi.fn() };
});

const item: Extract<TimelineItem, { kind: "mail_draft" }> = {
  item_id: "card-1", kind: "mail_draft", run_id: "run-1", operation_id: "op-1", created_at: "2026-09-14T00:00:00Z",
  draft: {
    operation_id: "op-1", kind: "new", version: 3, status: "pending",
    to: ["office@example.edu"], subject: "证明文件", body: "完整正文", attachments: [],
  },
  execution: { operation_id: "op-1", version: 3, status: "pending", confirmation: null, result: null },
};

beforeEach(() => { editDraft.mockReset(); editDraft.mockResolvedValue({ version: 4 }); });
afterEach(cleanup);

test("卡片修改框发送绑定 operation_id 的新一轮对话", async () => {
  const sendMessage = vi.fn().mockResolvedValue(null);
  render(<MailDraftCard taskId="task-1" item={item} sendMessage={sendMessage} onChanged={vi.fn()} />);
  await userEvent.type(screen.getByLabelText("针对这封邮件提出修改要求"), "语气更正式{enter}");
  expect(sendMessage).toHaveBeenCalledWith("语气更正式", { kind: "mail_draft", operation_id: "op-1" });
});

test("直接编辑按当前版本保存，版本冲突时保留本地内容", async () => {
  editDraft.mockRejectedValue(new ApiError("version_conflict", "当前版本为 4", 409, 4));
  render(<MailDraftCard taskId="task-1" item={item} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "编辑" }));
  const subject = screen.getByLabelText("主题");
  await userEvent.clear(subject);
  await userEvent.type(subject, "更正式的主题");
  expect((screen.getByLabelText("针对这封邮件提出修改要求") as HTMLInputElement).disabled).toBe(true);
  await userEvent.click(screen.getByRole("button", { name: "保存" }));
  expect(editDraft).toHaveBeenCalledWith("op-1", 3, {
    to: ["office@example.edu"], subject: "更正式的主题", body: "完整正文", attachment_ids: [],
  });
  expect((screen.getByLabelText("主题") as HTMLInputElement).value).toBe("更正式的主题");
  expect(screen.getByText("草稿已被更新，你的修改尚未保存")).toBeTruthy();
});

test("取消编辑会丢弃本地值并重新读取服务端时间线", async () => {
  const onChanged = vi.fn().mockResolvedValue(undefined);
  render(<MailDraftCard taskId="task-1" item={item} sendMessage={vi.fn()} onChanged={onChanged} />);
  await userEvent.click(screen.getByRole("button", { name: "编辑" }));
  await userEvent.clear(screen.getByLabelText("主题"));
  await userEvent.type(screen.getByLabelText("主题"), "尚未保存");
  await userEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(onChanged).toHaveBeenCalledOnce();
  expect(screen.getByText("证明文件")).toBeTruthy();
});

test("发送中、成功、失败和待核实都在原卡片呈现", () => {
  const confirmation = { task_id: "task-1", version: 3, confirmed_at: "2026-09-14T00:00:00Z" };
  const { rerender } = render(<MailDraftCard taskId="task-1" item={{
    ...item, execution: { ...item.execution, status: "sending", confirmation },
  }} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.getByText("已确认，正在发送。内容已经锁定。")).toBeTruthy();
  expect((screen.getByRole("button", { name: "编辑" }) as HTMLButtonElement).disabled).toBe(true);

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
