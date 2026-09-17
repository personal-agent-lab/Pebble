// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

import type { TimelineItem } from "../api";
import TimelineFeed from "./TimelineFeed";

const scrollIntoView = vi.fn();
beforeAll(() => { Element.prototype.scrollIntoView = scrollIntoView; });
beforeEach(() => { scrollIntoView.mockClear(); });
afterEach(cleanup);

const draft: Extract<TimelineItem, { kind: "mail_draft" }> = {
  item_id: "card-1",
  kind: "mail_draft",
  run_id: "run-1",
  operation_id: "op-1",
  created_at: "2026-09-14T00:00:00Z",
  draft: {
    operation_id: "op-1", kind: "new", version: 1, status: "pending",
    to: ["office@example.edu"], subject: "证明文件", body: "请查收完整证明文件。",
  },
  execution: { operation_id: "op-1", version: 1, status: "pending", confirmation: null, result: null },
};

test("按持久化顺序渲染 Agent 文字、完整邮件卡和后续文字", () => {
  const items: TimelineItem[] = [
    { item_id: "text-1", kind: "text", role: "assistant", run_id: "run-1", text: "前置说明", created_at: "2026-09-14T00:00:00Z" },
    draft,
    { item_id: "text-2", kind: "text", role: "assistant", run_id: "run-1", text: "后置说明", created_at: "2026-09-14T00:00:01Z" },
  ];
  const { container } = render(<TimelineFeed taskId="task-1" items={items} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);
  const text = container.textContent ?? "";
  expect(text.indexOf("前置说明")).toBeLessThan(text.indexOf("证明文件"));
  expect(text.indexOf("证明文件")).toBeLessThan(text.indexOf("后置说明"));
  expect(screen.getByText("请查收完整证明文件。")).toBeTruthy();
});

test("只有时间线出现新内容才贴底，重渲染本身不滚动", () => {
  const items: TimelineItem[] = [
    { item_id: "text-1", kind: "text", role: "assistant", run_id: "run-1", text: "前置说明", created_at: "2026-09-14T00:00:00Z" },
  ];
  const props = { taskId: "task-1", running: false, sendMessage: vi.fn(), onChanged: vi.fn() };
  const { rerender } = render(<TimelineFeed {...props} items={items} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);

  // 任务列表轮询、输入框打字都会让父组件重渲染，但时间线没变，不该动滚动位置。
  rerender(<TimelineFeed {...props} items={items} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);

  rerender(<TimelineFeed {...props} items={[...items, {
    item_id: "text-2", kind: "text", role: "assistant", run_id: "run-1",
    text: "后置说明", created_at: "2026-09-14T00:00:01Z",
  }]} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(2);
});

test("末条文字流式增长时继续贴底", () => {
  const props = { taskId: "task-1", running: false, sendMessage: vi.fn(), onChanged: vi.fn() };
  const partial: TimelineItem = {
    item_id: "text-1", kind: "text", role: "assistant", run_id: "run-1",
    text: "邮件草稿", created_at: "2026-09-14T00:00:00Z",
  };
  const { rerender } = render(<TimelineFeed {...props} items={[partial]} />);
  rerender(<TimelineFeed {...props} items={[{ ...partial, text: "邮件草稿已准备好" }]} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(2);
});

const answer = (id: string, text: string, createdAt: string): TimelineItem =>
  ({ item_id: id, kind: "text", role: "assistant", run_id: "run-1", text, created_at: createdAt });

test("连续几条回答只在末尾留一处落款，复制拿到整段而不是最后一截", async () => {
  const writeText = vi.fn(async () => {});
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "帮我排日程", created_at: "2026-09-14T00:00:00Z" },
    answer("text-1", "先查冲突", "2026-09-14T00:00:01Z"),
    answer("text-2", "没有冲突，已创建", "2026-09-14T00:00:02Z"),
  ];
  render(<TimelineFeed taskId="task-1" items={items} running={false} sendMessage={vi.fn()} onChanged={vi.fn()} />);

  const copy = screen.getByRole("button", { name: "复制回答" });
  await userEvent.click(copy);
  expect(writeText).toHaveBeenCalledWith("先查冲突\n\n没有冲突，已创建");
  expect(await screen.findByRole("button", { name: "已复制" })).toBeTruthy();
});

test("回答还在流式输出时不挂落款，避免复制到半截文字", () => {
  const items = [answer("text-1", "正在查冲突", "2026-09-14T00:00:01Z")];
  const { rerender } = render(<TimelineFeed taskId="task-1" items={items} running={true}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "复制回答" })).toBeNull();

  rerender(<TimelineFeed taskId="task-1" items={items} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.getByRole("button", { name: "复制回答" })).toBeTruthy();
});

test("用户消息与邮件卡不挂落款", () => {
  const items: TimelineItem[] = [
    { item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "帮我排日程", created_at: "2026-09-14T00:00:00Z" },
    draft,
  ];
  render(<TimelineFeed taskId="task-1" items={items} running={false} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "复制回答" })).toBeNull();
});

test("附件单独消息刷新后仍按顺序显示图片与下载文件", () => {
  const items: TimelineItem[] = [{
    item_id: "ask", kind: "text", role: "user", run_id: "run-1", text: "",
    created_at: "2026-09-14T00:00:00Z", attachments: [
      { file_id: "image", filename: "photo.png", mime_type: "image/png", size: 12,
        sha256: "a", url: "/api/tasks/t/attachments/image" },
      { file_id: "text", filename: "notes.md", mime_type: "text/markdown", size: 1024,
        sha256: "b", url: "/api/tasks/t/attachments/text" },
    ],
  }];
  render(<TimelineFeed taskId="task-1" items={items} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} />);

  expect(screen.getByRole("link", { name: "查看图片 photo.png" }).getAttribute("href"))
    .toBe("/api/tasks/t/attachments/image");
  const download = screen.getByText("notes.md").closest("a");
  expect(download?.getAttribute("href")).toBe("/api/tasks/t/attachments/text");
  expect(download?.hasAttribute("download")).toBe(true);
});

test("调用进行中在消息流末尾留思考占位，结束后撤掉", () => {
  const items: TimelineItem[] = [
    { item_id: "text-1", kind: "text", role: "user", run_id: "run-1", text: "帮我看邮件", created_at: "2026-09-14T00:00:00Z" },
  ];
  const props = { taskId: "task-1", items, sendMessage: vi.fn(), onChanged: vi.fn() };
  const { container, rerender } = render(<TimelineFeed {...props} running={true} />);
  expect(container.querySelectorAll(".thinking .dot").length).toBe(3);
  expect(screen.getByRole("status").textContent).toContain("Agent 正在处理");

  rerender(<TimelineFeed {...props} running={false} />);
  expect(container.querySelector(".thinking")).toBeNull();
});

test("从搜索结果跳进来时滚到命中条目并高亮，之后的新内容不再拽回底部", () => {
  const items: TimelineItem[] = [
    { item_id: "text-1", kind: "text", role: "user", run_id: "run-1", text: "预算怎么定", created_at: "2026-09-14T00:00:00Z" },
    { item_id: "text-2", kind: "text", role: "assistant", run_id: "run-1", text: "提高一成", created_at: "2026-09-14T00:00:01Z" },
    draft,
  ];
  const props = { taskId: "task-1", running: false, sendMessage: vi.fn(), onChanged: vi.fn() };
  const { container, rerender } = render(<TimelineFeed {...props} items={items} focusItemId="text-2" />);

  const target = container.querySelector("#item-text-2");
  expect(target?.getAttribute("data-focus")).toBe("true");
  expect(scrollIntoView).toHaveBeenCalledTimes(1);
  expect(scrollIntoView.mock.contexts[0]).toBe(target);
  // 邮件卡也有可定位的锚点
  expect(container.querySelector("#item-card-1")).toBeTruthy();

  rerender(<TimelineFeed {...props} focusItemId="text-2" items={[...items, {
    item_id: "text-3", kind: "text", role: "assistant", run_id: "run-2",
    text: "新回答", created_at: "2026-09-14T00:00:02Z",
  }]} />);
  expect(scrollIntoView).toHaveBeenCalledTimes(1);
});

test("处理中显示当前步骤；没有步骤时只有跳动的点", () => {
  const props = { taskId: "task-1", items: [] as TimelineItem[], sendMessage: vi.fn(), onChanged: vi.fn() };
  const { rerender } = render(<TimelineFeed {...props} running activity={null} />);
  expect(screen.getByRole("status").textContent).toBe("Agent 正在处理");

  rerender(<TimelineFeed {...props} running activity="正在检索资料：星云验收" />);
  expect(screen.getByRole("status").textContent).toBe("正在检索资料：星云验收");

  rerender(<TimelineFeed {...props} running={false} activity="正在检索资料：星云验收" />);
  expect(screen.queryByRole("status")).toBeNull();
});

test("记忆变更提示带查看记忆入口，其他提示不带", () => {
  const notice = (item_id: string, text: string): TimelineItem => ({
    item_id, kind: "notice", run_id: "run-1", text, created_at: "2026-09-14T00:00:00Z",
  });
  const items = [
    notice("n-1", "已记住：回答先给结论"),
    notice("n-2", "整理记忆：已删除：重复的约定"),
    notice("n-3", "想确认：你说的是哪一条？"),
    notice("n-4", "已修改资料：周会纪要。位置：kb/inbox/周会.md。版本：a → b"),
  ];
  render(<MemoryRouter><TimelineFeed taskId="task-1" items={items} running={false}
    sendMessage={vi.fn()} onChanged={vi.fn()} /></MemoryRouter>);

  const links = screen.getAllByRole("link", { name: "查看记忆" });
  expect(links).toHaveLength(2);
  expect(links.every((link) => link.getAttribute("href") === "/memory")).toBe(true);
  expect(screen.getByText("想确认：你说的是哪一条？").parentElement?.querySelector("a")).toBeNull();
});
