// @vitest-environment jsdom

import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { ApiError, type AgentEvent, type Run, type TaskDetail, type TimelineItem } from "./api";

const { useNote, useTaskDetail } = await import("./hooks");

let onEvent: ((event: AgentEvent) => void) | null = null;
let latestRun: Run | null = null;
let timelineItems: TimelineItem[] = [];
const interruptTask = vi.fn();

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getTask: (taskId: string) => Promise.resolve({
      task_id: taskId, goal: "查资料", model: "Auto", source: "user", sdk_session_id: null,
      created_at: "2026-09-16T00:00:00Z", latest_run: latestRun,
    } satisfies TaskDetail),
    listOperations: () => Promise.resolve([]),
    getTimeline: (taskId: string) => Promise.resolve({ task_id: taskId, sdk_session_id: null, items: timelineItems }),
    interruptTask: (...args: unknown[]) => interruptTask(...args),
    subscribeEvents: (_taskId: string, handler: (event: AgentEvent) => void) => {
      onEvent = handler;
      return () => undefined;
    },
  };
});

afterEach(() => {
  cleanup();
  // 假定时器只在那一条用例里用；不还原会让后面的 waitFor 永远等不到。
  vi.useRealTimers();
  onEvent = null;
  latestRun = null;
  timelineItems = [];
  interruptTask.mockReset();
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

test("工具写入事件在轮次结束前刷新时间线", async () => {
  latestRun = running(null);
  const { result } = renderHook(() => useTaskDetail("task-1"));
  await waitFor(() => expect(result.current.task?.latest_run?.status).toBe("running"));
  timelineItems = [{
    item_id: "tool-1", kind: "tool", run_id: "run-1", tool_call_id: "call-1",
    name: "calendar_list_events", arguments: {}, status: "running", result: null,
    created_at: "2026-09-24T10:22:33Z",
  }];
  act(() => onEvent?.({ type: "timeline_changed", run_id: "run-1" }));
  await waitFor(() => expect(result.current.items).toEqual(timelineItems));
  expect(result.current.task?.latest_run?.status).toBe("running");
});

test("操作提示停留 3 秒后自动收起；置空不设定时器", () => {
  vi.useFakeTimers();
  const { result } = renderHook(() => useNote());

  act(() => result.current[1]("已保存"));
  expect(result.current[0]).toBe("已保存");

  act(() => vi.advanceTimersByTime(3000));
  expect(result.current[0]).toBeNull();

  act(() => result.current[1](null));
  act(() => vi.advanceTimersByTime(10_000));
  expect(result.current[0]).toBeNull();
});

test("终止调用后台接口并重读这一轮，结束后按钮状态复位", async () => {
  latestRun = running(null);
  interruptTask.mockImplementation(async (taskId: string) => {
    latestRun = {
      ...running(null), task_id: taskId, status: "interrupted",
      error: "你已终止这一轮执行", finished_at: "2026-09-16T00:00:09Z",
    };
    return latestRun;
  });
  const { result } = renderHook(() => useTaskDetail("task-1"));
  await waitFor(() => expect(result.current.task?.latest_run?.status).toBe("running"));

  const seen: { failure: ApiError | null } = { failure: null };
  await act(async () => { seen.failure = await result.current.stop(); });

  expect(seen.failure).toBeNull();
  expect(interruptTask).toHaveBeenCalledWith("task-1");
  expect(result.current.task?.latest_run?.status).toBe("interrupted");
  expect(result.current.stopping).toBe(false);
});

test("终止时这一轮已经自己结束：按最新状态对齐，不向用户报错", async () => {
  latestRun = running(null);
  interruptTask.mockRejectedValue(
    new ApiError("task_not_running", "任务当前没有进行中的调用", 409),
  );
  const { result } = renderHook(() => useTaskDetail("task-1"));
  await waitFor(() => expect(result.current.task?.latest_run?.status).toBe("running"));

  latestRun = { ...running(null), status: "done", finished_at: "2026-09-16T00:00:09Z" };
  const seen: { failure: ApiError | null } = { failure: null };
  await act(async () => { seen.failure = await result.current.stop(); });

  expect(seen.failure).toBeNull();
  expect(result.current.task?.latest_run?.status).toBe("done");
  expect(result.current.stopping).toBe(false);
});

test("终止失败把原因交回页面", async () => {
  latestRun = running(null);
  interruptTask.mockRejectedValue(new ApiError("offline", "无法连接 Pebble 服务", 0));
  const { result } = renderHook(() => useTaskDetail("task-1"));
  await waitFor(() => expect(result.current.task?.latest_run?.status).toBe("running"));

  const seen: { failure: ApiError | null } = { failure: null };
  await act(async () => { seen.failure = await result.current.stop(); });

  expect(seen.failure?.message).toBe("无法连接 Pebble 服务");
  expect(result.current.stopping).toBe(false);
});
