/** 已保存时间线与实时事件的唯一前端读取 Module。 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type AgentEvent,
  type MessageTarget,
  type OperationSummary,
  type Run,
  type Task,
  type TaskDetail,
  type TimelineItem,
  getTask,
  getTimeline,
  listOperations,
  listTasks,
  sendMessage,
  subscribeEvents,
} from "./api";

const LIST_POLL_MS = 5000;
const EXECUTION_POLL_MS = 1500;
const toApiError = (error: unknown) =>
  error instanceof ApiError ? error : new ApiError("offline", String(error), 0);

function usePolling(run: () => void, intervalMs: number, enabled: boolean): void {
  const latest = useRef(run);
  latest.current = run;
  useEffect(() => {
    if (!enabled) return;
    let timer: number | undefined;
    const stop = () => {
      if (timer !== undefined) window.clearInterval(timer);
      timer = undefined;
    };
    const start = () => {
      stop();
      timer = window.setInterval(() => latest.current(), intervalMs);
    };
    const onVisibility = () => {
      if (document.hidden) stop();
      else { latest.current(); start(); }
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => { stop(); document.removeEventListener("visibilitychange", onVisibility); };
  }, [intervalMs, enabled]);
}

export type TaskEntry = { task: Task; latestRun: Run | null; operations: OperationSummary[] };
export function useTaskList() {
  const [entries, setEntries] = useState<TaskEntry[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const reload = useCallback(async () => {
    try {
      const tasks = await listTasks();
      setEntries(await Promise.all(tasks.map(async (task): Promise<TaskEntry> => {
        const [detail, operations] = await Promise.all([getTask(task.task_id), listOperations(task.task_id)]);
        return { task: detail, latestRun: detail.latest_run, operations };
      })));
      setError(null);
    } catch (failure) { setError(toApiError(failure)); }
  }, []);
  useEffect(() => void reload(), [reload]);
  usePolling(() => void reload(), LIST_POLL_MS, true);
  return { entries, error, reload };
}

export function useTaskDetail(taskId: string) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [operations, setOperations] = useState<OperationSummary[]>([]);
  const [items, setItems] = useState<TimelineItem[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [sending, setSending] = useState(false);

  const reload = useCallback(async () => {
    try {
      const [detail, loadedOperations, timeline] = await Promise.all([
        getTask(taskId), listOperations(taskId), getTimeline(taskId),
      ]);
      setTask(detail);
      setOperations(loadedOperations);
      setItems(timeline.items);
      setError(null);
    } catch (failure) { setError(toApiError(failure)); }
  }, [taskId]);
  useEffect(() => void reload(), [reload]);

  useEffect(() => {
    const onEvent = (event: AgentEvent) => {
      if (event.type === "session") {
        setTask((current) => current === null ? current : { ...current, sdk_session_id: event.sdk_session_id });
        return;
      }
      if (event.type === "text") {
        setItems((current) => {
          const index = current.findIndex((item) => item.item_id === event.item_id);
          if (index === -1) return [...current, {
            item_id: event.item_id,
            kind: "text",
            role: "assistant",
            run_id: event.run_id,
            text: event.text,
            created_at: new Date().toISOString(),
          }];
          const item = current[index];
          if (item.kind !== "text") return current;
          const next = [...current];
          next[index] = { ...item, text: item.text + event.text };
          return next;
        });
        return;
      }
      void reload();
    };
    return subscribeEvents(taskId, onEvent, () => void reload());
  }, [taskId, reload]);

  const executing = items.some((item) => item.kind === "mail_draft" && ["sending", "creating"].includes(item.execution.status));
  usePolling(() => void reload(), EXECUTION_POLL_MS, executing);

  const send = useCallback(async (
    message: string,
    target: MessageTarget | null = null,
  ) => {
    setSending(true);
    try {
      await sendMessage(taskId, message, target);
      await reload();
      return null;
    } catch (failure) { return toApiError(failure); }
    finally { setSending(false); }
  }, [taskId, reload]);

  return { task, operations, items, error, sending, send, reload };
}
