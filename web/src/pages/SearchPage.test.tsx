// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

const search = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve([]),
    searchHistory: (q: string) => search(q),
  };
});

const App = (await import("../App")).default;

afterEach(() => {
  cleanup();
  search.mockReset();
});

test("搜索历史对话，结果显示任务、说话方与片段，并链接到原对话的命中位置", async () => {
  search.mockResolvedValue({
    query: "预算评审",
    results: [
      {
        task_id: "task-9", task_title: "预算评审邮件", item_id: "item-3", speaker: "mail_draft",
        created_at: "2026-09-10T02:00:00Z", snippet: "主题：预算评审 改到下周二上午",
      },
    ],
  });
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/search"]}><App /></MemoryRouter>);

  await user.type(screen.getByRole("searchbox", { name: "搜索对话" }), "预算评审");

  const link = await screen.findByRole("link", { name: /预算评审邮件/ });
  expect(search).toHaveBeenCalledWith("预算评审");
  expect(link.getAttribute("href")).toBe("/tasks/task-9#item-item-3");
  expect(link.textContent).toContain("邮件草稿");
  expect(link.textContent).toContain("改到下周二上午");
});

test("没有命中时明确说没有找到", async () => {
  search.mockResolvedValue({ query: "火星", results: [] });
  render(<MemoryRouter initialEntries={["/search?q=火星"]}><App /></MemoryRouter>);
  expect(await screen.findByText("没有找到相关对话")).toBeTruthy();
});
