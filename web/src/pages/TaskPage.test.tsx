// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, expect, test, vi } from "vitest";

import { ApiError, type Run, type Task, type TaskDetail } from "../api";

const createTask = vi.fn();
const getTask = vi.fn();
let created: string[] = [];

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve(created.map((taskId) => task(taskId))),
    listModels: () => Promise.resolve({
      default_model: "model-a", models: [{ id: "model-a", label: "Model A", kind: "managed" }],
      fetched_at: "2026-09-18T00:00:00Z", stale: false,
    }),
    createTask: (...args: unknown[]) => createTask(...args),
    getTask: (taskId: string) => getTask(taskId),
    listOperations: () => Promise.resolve([]),
    getTimeline: (taskId: string) => Promise.resolve({
      task_id: taskId, sdk_session_id: null,
      items: [{ item_id: "u1", kind: "text", role: "user", run_id: "run-1", text: "服务端的消息", created_at: "2026-09-18T00:00:00Z" }],
    }),
    subscribeEvents: () => () => undefined,
  };
});

const App = (await import("../App")).default;
beforeAll(() => { Element.prototype.scrollIntoView = () => undefined; });
const { resetPendingTasks } = await import("../pendingTasks");

const run = (taskId: string): Run => ({
  run_id: "run-1", task_id: taskId, kind: "message", status: "running", error: null,
  created_at: "2026-09-18T00:00:00Z", started_at: null, finished_at: null, activity: null,
});
const task = (taskId: string): TaskDetail => ({
  task_id: taskId, goal: "解释 JIT", model: "model-a", source: "user", sdk_session_id: null,
  created_at: "2026-09-18T00:00:00Z", latest_run: run(taskId),
} as TaskDetail);

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

afterEach(() => {
  cleanup();
  createTask.mockReset();
  getTask.mockReset();
  created = [];
  resetPendingTasks();
});

async function send(text: string) {
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/tasks"]}><App /></MemoryRouter>);
  await user.type(screen.getByRole("textbox", { name: "消息" }), text);
  // 模型目录读到后发送才可用。
  await waitFor(() => expect((screen.getByRole("button", { name: "发送" }) as HTMLButtonElement).disabled).toBe(false));
  await user.click(screen.getByRole("button", { name: "发送" }));
  return user;
}

test("按下发送立即进入任务页显示消息，创建成功后才读取服务端任务", async () => {
  const pending = deferred<{ task: Task; run: Run }>();
  createTask.mockReturnValue(pending.promise);
  getTask.mockImplementation((taskId: string) => Promise.resolve(task(taskId)));

  await send("解释 JIT");

  expect(await screen.findByText("解释 JIT", { selector: ".bubble" })).toBeTruthy();
  expect(screen.getByText("正在发送…")).toBeTruthy();
  expect(getTask).not.toHaveBeenCalled();
  const [message, model, files, taskId] = createTask.mock.calls[0];
  expect([message, model, files]).toEqual(["解释 JIT", "model-a", []]);
  expect(taskId).toMatch(/^[0-9a-f-]{36}$/);

  created = [taskId];
  pending.resolve({ task: task(taskId), run: run(taskId) });
  expect(await screen.findByText("服务端的消息")).toBeTruthy();
  expect(getTask).toHaveBeenCalledWith(taskId);
});

test("连接失败可原样重试，重试沿用同一任务标识", async () => {
  createTask.mockRejectedValueOnce(new ApiError("offline", "无法连接 Pebble 服务", 0));
  getTask.mockImplementation((taskId: string) => Promise.resolve(task(taskId)));

  const user = await send("解释 JIT");

  expect(await screen.findByText(/发送失败：无法连接 Pebble 服务/)).toBeTruthy();
  const firstId = createTask.mock.calls[0][3];
  createTask.mockResolvedValueOnce({ task: task(firstId), run: run(firstId) });
  await user.click(screen.getByRole("button", { name: "重试" }));

  expect(await screen.findByText("服务端的消息")).toBeTruthy();
  expect(createTask.mock.calls[1][3]).toBe(firstId);
});

test("输入被拒绝不提供重试，编辑后重发把文字放回首页输入框", async () => {
  createTask.mockRejectedValueOnce(new ApiError("invalid_model", "模型当前不可用：model-a", 422));

  const user = await send("解释 JIT");

  expect(await screen.findByText(/发送失败：模型当前不可用/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "编辑后重发" }));

  await waitFor(() => expect(
    (screen.getByRole("textbox", { name: "消息" }) as HTMLTextAreaElement).value,
  ).toBe("解释 JIT"));
  expect(getTask).not.toHaveBeenCalled();
});

test("离开后才失败的消息不冲掉首页正在写的内容，由用户选择放回", async () => {
  const pending = deferred<{ task: Task; run: Run }>();
  createTask.mockReturnValue(pending.promise);

  const user = await send("解释 JIT");
  await screen.findByText("正在发送…");
  await user.click(screen.getByRole("button", { name: "返回任务列表" }));
  const input = await screen.findByRole("textbox", { name: "消息" }) as HTMLTextAreaElement;
  await user.type(input, "新的问题");

  pending.reject(new ApiError("offline", "无法连接 Pebble 服务", 0));
  expect(await screen.findByText("上一条消息没有发出")).toBeTruthy();
  expect(input.value).toBe("新的问题");

  await user.click(screen.getByRole("button", { name: "放回输入框" }));
  await waitFor(() => expect(
    (screen.getByRole("textbox", { name: "消息" }) as HTMLTextAreaElement).value,
  ).toBe("解释 JIT"));
});
