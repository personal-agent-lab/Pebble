/**
 * 任务列表的单一读取点。
 *
 * 侧栏任务列表与任务总览页展示同一份数据：由 Provider 统一读取与轮询，
 * 避免两处各轮询一遍，也避免侧栏与正文出现不同步的两份状态。
 */

import { createContext, useContext, type ReactNode } from "react";

import type { ApiError } from "./api";
import { useTaskList, type TaskEntry } from "./hooks";

type TaskListValue = {
  entries: TaskEntry[] | null;
  error: ApiError | null;
  reload: () => Promise<void>;
};

const TaskListContext = createContext<TaskListValue | null>(null);

export function TaskListProvider({ children }: { children: ReactNode }) {
  const value = useTaskList();
  return <TaskListContext.Provider value={value}>{children}</TaskListContext.Provider>;
}

export function useTasks(): TaskListValue {
  const value = useContext(TaskListContext);
  if (value === null) throw new Error("useTasks 必须在 TaskListProvider 内使用");
  return value;
}

/** 全部任务中待确认操作的总数；列表未读到时按 0 计，不显示占位数字。 */
export function pendingTotal(entries: TaskEntry[] | null): number {
  return (
    entries?.reduce(
      (total, entry) =>
        total + entry.operations.filter((operation) => operation.status === "pending").length,
      0,
    ) ?? 0
  );
}
