// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { AgentEvent, Run, TaskDetail } from "./api";

let onEvent: ((event: AgentEvent) => void) | null = null;
let latestRun: Run | null = null;

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getTask: (taskId: string) => Promise.resolve({
      task_id: taskId, goal: "查资料", model: "Auto", source: "user", sdk_session_id: null,
      created_at: "2026-09-16T00:00:00Z", latest_run: latestRun,
    } satisfies TaskDetail),
    listOperations: () => Promise.resolve([]),
    getTimeline: (taskId: string) => Promise.resolve({ task_id: taskId, sdk_session_id: null, items: [] }),
    subscribeEvents: (_taskId: string, handler: (event: AgentEvent) => void) => {
      onEvent = handler;
      return () => undefined;
    },
  };
});

const { useTaskDetail } = await import("./hooks");

afterEach(() => {
  cleanup();
  onEvent = null;
  latestRun = null;
});

const running = (activity: string | null): Run => ({
  run_id: "run-1", task_id: "task-1", kind: "message", status: "running", error: null,
  created_at: "2026-09-16T00:00:00Z", started_at: "2026-09-16T00:00:01Z", finished_at: null, activity,
});

test("步骤事件实时显示，Agent 开始写回答就收起；重读时沿用服务端记住的步骤", async () => {
  latestRun = running("正在读取资料：项目/验收.md");
  const { result } = renderHook(() => useTaskDetail("task-1"));
  await waitFor(() => expect(result.current.activity).toBe("正在读取资料：项目/验收.md"));

  act(() => onEvent?.({ type: "activity", run_id: "run-1", text: "正在检索资料：星云验收" }));
  expect(result.current.activity).toBe("正在检索资料：星云验收");

  act(() => onEvent?.({ type: "text", run_id: "run-1", item_id: "text-1", text: "找到了" }));
  expect(result.current.activity).toBeNull();
});
