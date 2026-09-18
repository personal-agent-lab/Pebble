// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, expect, test, vi } from "vitest";

import type { OperationSummary, Run, Task, TaskDetail, Timeline } from "../api";

const tasks: Task[] = [
  { task_id: "task-1", goal: "没读过的任务", model: "Auto", source: "mail", sdk_session_id: null, created_at: "2026-09-14T01:00:00Z" },
  { task_id: "task-2", goal: "正在看的任务", model: "Auto", source: "user", sdk_session_id: null, created_at: "2026-09-14T02:00:00Z" },
];
const operations: Record<string, OperationSummary[]> = {
  "task-1": [{ operation_id: "op-1", type: "mail_draft", version: 1, status: "pending" }],
  "task-2": [{ operation_id: "op-2", type: "mail_draft", version: 1, status: "pending" }],
};
const runs: Record<string, Run | null> = {};
const doneRun = (taskId: string, runId: string): Run => ({
  run_id: runId, task_id: taskId, kind: "new_mail", status: "done", error: null,
  created_at: "2026-09-14T03:00:00Z", started_at: null, finished_at: "2026-09-14T03:01:00Z",
});

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve(tasks),
    getTask: (taskId: string) =>
      Promise.resolve({
        ...(tasks.find((task) => task.task_id === taskId) as Task),
        latest_run: runs[taskId] ?? null,
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
  for (const key of Object.keys(runs)) delete runs[key];
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

const rowOf = (goal: string) =>
  [...document.querySelectorAll(ROW)].find((node) => node.textContent === goal)?.closest("a") ?? null;

const dotOf = (goal: string) => rowOf(goal)?.querySelector(".nav-dot") ?? null;

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

test("邮件触发的任务在列表里带邮件标记，自己发起的只留空槽位", async () => {
  await open("/tasks");

  expect(rowOf("没读过的任务")?.querySelector(".nav-task-source svg")).toBeTruthy();
  expect(rowOf("没读过的任务")?.textContent).toContain("由新邮件触发");
  // 槽位照留，任务名才对得齐；标记本身不出现。
  expect(rowOf("正在看的任务")?.querySelector(".nav-task-source")).toBeTruthy();
  expect(rowOf("正在看的任务")?.querySelector(".nav-task-source svg")).toBeNull();
  expect(rowOf("正在看的任务")?.textContent).not.toContain("由新邮件触发");
});

test("手机底部切到别的分区再点回任务，回到刚才打开的任务", async () => {
  await open("/tasks/task-2");
  const tasksTab = () => document.querySelector<HTMLAnchorElement>(".tabbar a[href^='/tasks']");
  // 已在任务分区内时，入口回到任务列表。
  expect(tasksTab()?.getAttribute("href")).toBe("/tasks");

  fireEvent.click(document.querySelector(".tabbar a[href='/memory']") as HTMLAnchorElement);
  await waitFor(() => expect(tasksTab()?.getAttribute("href")).toBe("/tasks/task-2"));
  expect(tasksTab()?.classList.contains("active")).toBe(false);

  fireEvent.click(tasksTab() as HTMLAnchorElement);
  await waitFor(() => expect(tasksTab()?.classList.contains("active")).toBe(true));
  expect(rowOf("正在看的任务")?.classList.contains("active")).toBe(true);
});

test("生成完还没看过的任务单独标记，打开后消失，待确认优先", async () => {
  // 已有提醒记录（不是首次启用），两条任务都有新结束的调用。
  window.localStorage.setItem("pebble.tasks.seenRuns", "{}");
  runs["task-1"] = doneRun("task-1", "run-1");
  runs["task-2"] = doneRun("task-2", "run-2");
  operations["task-2"] = [];
  await open("/tasks");

  expect(dotOf("正在看的任务")?.classList.contains("fresh")).toBe(true);
  expect(dotOf("没读过的任务")?.classList.contains("wait")).toBe(true);
  // 新结果不进“待确认”计数。
  expect(document.querySelector(".nav-head-count")?.textContent).toBe("1 待确认");
  cleanup();

  await open("/tasks/task-2");
  await waitFor(() => expect(dotOf("正在看的任务")).toBeNull());
});

test("首次启用时已有的结果都算读过", async () => {
  runs["task-2"] = doneRun("task-2", "run-2");
  operations["task-2"] = [];
  await open("/tasks");
  await waitFor(() => expect(window.localStorage.getItem("pebble.tasks.seenRuns")).not.toBeNull());
  expect(dotOf("正在看的任务")).toBeNull();
});
