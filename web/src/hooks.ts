/** 前端共享 hooks：已保存时间线与实时事件的读取，以及各页共用的操作结果提示。 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type AgentEvent,
  type MessageTarget,
  type OperationSummary,
  type Run,
  type RunObservation,
  type SkillSelection,
  type SkillSummary,
  type Task,
  type TaskDetail,
  type TimelineItem,
  getObservations,
  getTask,
  getTimeline,
  listOperations,
  listSkills,
  listTasks,
  sendMessage,
  retryLastMessage,
  subscribeEvents,
} from "./api";

const LIST_POLL_MS = 5000;
const EXECUTION_POLL_MS = 1500;

/** 操作结果提示是瞬时确认，各页共用同一个停留时长。 */
const NOTE_DISMISS_MS = 3000;

/** 操作结果的一句话提示：setNote 写入后停留 3 秒自动收起，旧结果不留在页面上。 */
export function useNote(): [string | null, (message: string | null) => void] {
  const [note, setNote] = useState<string | null>(null);
  useEffect(() => {
    if (note === null) return;
    const timer = window.setTimeout(() => setNote(null), NOTE_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [note]);
  return [note, setNote];
}

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

/** 启用中的技能目录：输入框的技能选择用；读不到时不显示选择器，不影响发消息。 */
export function useSkillCatalog(): SkillSummary[] | null {
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  useEffect(() => {
    void listSkills().then(setSkills).catch(() => setSkills([]));
  }, []);
  return skills;
}
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
  const [retrying, setRetrying] = useState(false);
  // 当前步骤只来自实时事件与重读时服务端记住的那一步，不进时间线。
  const [activity, setActivity] = useState<string | null>(null);
  // 运行观测：读取失败不影响对话主界面，只收起执行详情。
  const [observations, setObservations] = useState<RunObservation[]>([]);

  const reload = useCallback(async () => {
    try {
      const [detail, loadedOperations, timeline] = await Promise.all([
        getTask(taskId), listOperations(taskId), getTimeline(taskId),
      ]);
      setTask(detail);
      setActivity(detail.latest_run?.activity ?? null);
      setOperations(loadedOperations);
      setItems(timeline.items);
      setError(null);
    } catch (failure) { setError(toApiError(failure)); }
    void getObservations(taskId)
      .then((payload) => setObservations(payload.runs))
      .catch(() => undefined);
  }, [taskId]);
  useEffect(() => void reload(), [reload]);

  useEffect(() => {
    const onEvent = (event: AgentEvent) => {
      if (event.type === "session") {
        setTask((current) => current === null ? current : { ...current, sdk_session_id: event.sdk_session_id });
        return;
      }
      if (event.type === "activity") {
        setActivity(event.text);
        return;
      }
      if (event.type === "text") {
        setActivity(null);
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
    files: File[] = [],
    selection: SkillSelection | null = null,
  ) => {
    setSending(true);
    try {
      await sendMessage(taskId, message, target, files, selection);
      await reload();
      return null;
    } catch (failure) { return toApiError(failure); }
    finally { setSending(false); }
  }, [taskId, reload]);

  const retry = useCallback(async () => {
    setRetrying(true);
    try {
      await retryLastMessage(taskId);
      await reload();
      return null;
    } catch (failure) { return toApiError(failure); }
    finally { setRetrying(false); }
  }, [taskId, reload]);

  return { task, operations, items, observations, activity, error, sending, retrying, send, retry, reload };
}
