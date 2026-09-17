// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApiError, type MemorySnapshot } from "../api";

const getMemory = vi.fn();
const addMemoryEntry = vi.fn();
const updateMemoryEntry = vi.fn();
const removeMemoryEntry = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve([]),
    getMemory: () => getMemory(),
    addMemoryEntry: (...args: unknown[]) => addMemoryEntry(...args),
    updateMemoryEntry: (...args: unknown[]) => updateMemoryEntry(...args),
    removeMemoryEntry: (...args: unknown[]) => removeMemoryEntry(...args),
  };
});

const App = (await import("../App")).default;

const snapshot = (): MemorySnapshot => ({
  user: { entries: ["回答先给结论", "默认使用简体中文"], usage: { chars: 18, limit: 1375 }, version: "u1" },
  memory: { entries: [], usage: { chars: 0, limit: 2200 }, version: "m1" },
});

beforeEach(() => { getMemory.mockResolvedValue(snapshot()); });
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const open = async () => {
  render(<MemoryRouter initialEntries={["/memory"]}><App /></MemoryRouter>);
  await screen.findByText("回答先给结论");
};
const section = (title: string) => screen.getByRole("region", { name: title });

test("两个分区列出条目与容量，空分区给出说明", async () => {
  await open();

  const user = section("关于你");
  expect(within(user).getByText("默认使用简体中文")).toBeTruthy();
  expect(within(user).getByText("18 / 1375 字")).toBeTruthy();
  const memory = section("事实与约定");
  expect(within(memory).getByText(/还没有内容/)).toBeTruthy();
  expect(within(memory).getByText("0 / 2200 字")).toBeTruthy();
  expect(screen.getAllByRole("link", { name: /记忆/ })[0].getAttribute("href")).toBe("/memory");
});

test("编辑条目时带上读取时的版本，保存后显示新内容", async () => {
  updateMemoryEntry.mockResolvedValue({
    target: "user", changed: true, version: "u2",
    entries: ["回答先解释推导", "默认使用简体中文"], usage: { chars: 19, limit: 1375 },
  });
  const user = userEvent.setup();
  await open();

  const row = screen.getByText("回答先给结论").closest(".memory-entry") as HTMLElement;
  await user.click(within(row).getByRole("button", { name: "编辑" }));
  const input = screen.getByLabelText("编辑记忆内容");
  await user.clear(input);
  await user.type(input, "回答先解释推导");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(updateMemoryEntry).toHaveBeenCalledWith("user", "回答先给结论", "回答先解释推导", "u1");
  expect(await screen.findByText("回答先解释推导")).toBeTruthy();
  expect(screen.queryByText("回答先给结论")).toBeNull();
  expect(screen.getByText("19 / 1375 字")).toBeTruthy();
});

test("新增与删除：删除先确认", async () => {
  addMemoryEntry.mockResolvedValue({
    target: "memory", changed: true, version: "m2",
    entries: ["内部讨论默认 45 分钟"], usage: { chars: 12, limit: 2200 },
  });
  removeMemoryEntry.mockResolvedValue({
    target: "user", changed: true, version: "u2",
    entries: ["回答先给结论"], usage: { chars: 6, limit: 1375 },
  });
  const user = userEvent.setup();
  await open();

  await user.click(within(section("事实与约定")).getByRole("button", { name: "+ 新增一条" }));
  await user.type(screen.getByLabelText("新记忆内容"), "内部讨论默认 45 分钟");
  await user.click(screen.getByRole("button", { name: "保存" }));
  expect(addMemoryEntry).toHaveBeenCalledWith("memory", "内部讨论默认 45 分钟", "m1");
  expect(await screen.findByText("内部讨论默认 45 分钟")).toBeTruthy();

  const row = screen.getByText("默认使用简体中文").closest(".memory-entry") as HTMLElement;
  await user.click(within(row).getByRole("button", { name: "删除" }));
  expect(removeMemoryEntry).not.toHaveBeenCalled();
  expect(within(row).getByText(/原对话仍保留/)).toBeTruthy();
  await user.click(within(row).getByRole("button", { name: "确认删除" }));
  expect(removeMemoryEntry).toHaveBeenCalledWith("user", "默认使用简体中文", "u1");
  await waitFor(() => expect(screen.queryByText("默认使用简体中文")).toBeNull());
});

test("版本冲突时保留输入并提示重新载入", async () => {
  updateMemoryEntry.mockRejectedValue(new ApiError("version_conflict", "当前版本为 u9", 409, "u9"));
  const user = userEvent.setup();
  await open();

  const row = screen.getByText("回答先给结论").closest(".memory-entry") as HTMLElement;
  await user.click(within(row).getByRole("button", { name: "编辑" }));
  await user.type(screen.getByLabelText("编辑记忆内容"), "，再解释原因");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(await screen.findByText("记忆已被更新")).toBeTruthy();
  expect((screen.getByLabelText("编辑记忆内容") as HTMLTextAreaElement).value).toBe("回答先给结论，再解释原因");

  getMemory.mockResolvedValue({ ...snapshot(), user: { ...snapshot().user, version: "u9" } });
  await user.click(screen.getByRole("button", { name: "重新载入" }));
  await waitFor(() => expect(screen.queryByText("记忆已被更新")).toBeNull());
  expect(getMemory).toHaveBeenCalledTimes(2);
});

test("容量不足时说明原因，超出上限的分区标红", async () => {
  getMemory.mockResolvedValue({
    ...snapshot(),
    memory: { entries: ["很长的约定"], usage: { chars: 2300, limit: 2200 }, version: "m1" },
  });
  addMemoryEntry.mockRejectedValue(new ApiError(
    "memory_full", "“事实与约定”放不下：保存后需要 2310 个字符，上限为 2200", 422,
  ));
  const user = userEvent.setup();
  await open();

  expect(within(section("事实与约定")).getByText("超出上限 100 字 · 2300 / 2200 字")).toBeTruthy();
  await user.click(within(section("事实与约定")).getByRole("button", { name: "+ 新增一条" }));
  await user.type(screen.getByLabelText("新记忆内容"), "再加一条");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(await screen.findByText(/放不下.*先精简这条，或删除、合并其他条目/)).toBeTruthy();
});
