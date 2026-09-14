// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

import type { OperationSummary, Task, TaskDetail, Timeline } from "../api";

const tasks: Task[] = [
  { task_id: "task-1", goal: "没读过的任务", sdk_session_id: null, created_at: "2026-09-14T01:00:00Z" },
  { task_id: "task-2", goal: "正在看的任务", sdk_session_id: null, created_at: "2026-09-14T02:00:00Z" },
];
const operations: Record<string, OperationSummary[]> = {
  "task-1": [{ operation_id: "op-1", type: "mail_draft", version: 1, status: "pending" }],
  "task-2": [{ operation_id: "op-2", type: "mail_draft", version: 1, status: "pending" }],
};

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve(tasks),
    getTask: (taskId: string) =>
      Promise.resolve({
        ...(tasks.find((task) => task.task_id === taskId) as Task),
        latest_run: null,
      } satisfies TaskDetail),
    listOperations: (taskId: string) => Promise.resolve(operations[taskId] ?? []),
    getTimeline: (taskId: string) =>
      Promise.resolve({ task_id: taskId, sdk_session_id: null, items: [] } satisfies Timeline),
    subscribeEvents: () => () => undefined,
  };
});

const App = (await import("../App")).default;

beforeAll(() => { Element.prototype.scrollIntoView = vi.fn(); });
beforeEach(() => {
  window.localStorage.clear();
  operations["task-2"] = [{ operation_id: "op-2", type: "mail_draft", version: 1, status: "pending" }];
});
afterEach(cleanup);

// 任务名在正文和窄屏的那份列表里也会出现，查询一律限定在侧栏这一份上。
const ROW = ".sidebar .nav-task .t";

/** 渲染并等到侧栏列表读出来为止。 */
const open = async (path: string) => {
  render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);
  await screen.findByText("没读过的任务", { selector: ROW });
};

const dotOf = (goal: string) => {
  const label = [...document.querySelectorAll(ROW)].find((node) => node.textContent === goal);
  return label?.closest("a")?.querySelector(".nav-dot") ?? null;
};

test("打开的任务读过就不再标记，其余任务照旧提醒", async () => {
  await open("/tasks/task-2");

  // 读过的痕迹在列表读出来之后落下，标记随即消失。
  await waitFor(() => expect(dotOf("正在看的任务")).toBeNull());
  expect(dotOf("没读过的任务")).toBeTruthy();
  // 头部计数与圆点同源：只数没读过的待确认。
  expect(document.querySelector(".nav-head-count")?.textContent).toBe("1 待确认");
});

test("读过的痕迹留在本地，换个页面回来仍然不提醒", async () => {
  await open("/tasks/task-2");
  await waitFor(() => expect(dotOf("正在看的任务")).toBeNull());
  cleanup();

  await open("/tasks");
  expect(dotOf("正在看的任务")).toBeNull();
  expect(dotOf("没读过的任务")).toBeTruthy();
});

test("草稿改出新版本重新提醒，读过的是那一版不是那条操作", async () => {
  await open("/tasks/task-2");
  await waitFor(() => expect(dotOf("正在看的任务")).toBeNull());
  cleanup();

  operations["task-2"] = [{ operation_id: "op-2", type: "mail_draft", version: 2, status: "pending" }];
  await open("/tasks");
  expect(dotOf("正在看的任务")).toBeTruthy();
});
