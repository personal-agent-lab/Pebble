/**
 * 数据读取与实时合流。
 *
 * 约定：后台工作不依赖页面连接，页面只负责读取已保存状态并订阅事件；
 * 断线、刷新或重新进入都以重新读取为准，不在前端保存另一份任务状态。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  type AgentEvent,
  type Draft,
  type Execution,
  type HistoryMessage,
  type OperationStatus,
  type OperationSummary,
  type Run,
  type Task,
  type TaskDetail,
  getDraft,
  getExecution,
  getHistory,
  getTask,
  listOperations,
  listTasks,
  sendMessage,
  subscribeEvents,
} from "./api";

const LIST_POLL_MS = 5000;
const EXECUTION_POLL_MS = 1500;

function toApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError("offline", String(error), 0);
}

/** 定时重复执行；页面不可见时暂停，重新可见时立即补一次。 */
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
      if (document.hidden) {
        stop();
      } else {
        latest.current();
        start();
      }
    };

    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [intervalMs, enabled]);
}

export type TaskEntry = { task: Task; latestRun: Run | null; operations: OperationSummary[] };

/**
 * 任务列表。后端没有任务级状态，也没有列表级事件流：逐个任务补读详情与操作，
 * 并按固定间隔轮询，让新邮件自动触发的任务无需手动刷新即可出现。
 */
export function useTaskList() {
  const [entries, setEntries] = useState<TaskEntry[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const reload = useCallback(async () => {
    try {
      const tasks = await listTasks();
      const loaded = await Promise.all(
        tasks.map(async (task): Promise<TaskEntry> => {
          const [detail, operations] = await Promise.all([
            getTask(task.task_id),
            listOperations(task.task_id),
          ]);
          return { task: detail, latestRun: detail.latest_run, operations };
        }),
      );
      setEntries(loaded);
      setError(null);
    } catch (failure) {
      setError(toApiError(failure));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  usePolling(() => void reload(), LIST_POLL_MS, true);

  return { entries, error, reload };
}

export type FeedItem =
  | { kind: "message"; id: string; role: "user" | "assistant"; text: string; streaming: boolean }
  | { kind: "system"; id: string; text: string }
  | { kind: "failure"; id: string; text: string };

type LiveItem = FeedItem & { runId: string | null; keep: boolean };

/**
 * 单个任务：已保存记录 + 实时事件。
 *
 * 历史由 Agent 会话返回（契约第 9 节），未接入 SDK 时为 unavailable；此时仍展示本轮
 * 事件与已保存的操作，不把依赖缺失当作任务故障。
 */
export function useTaskDetail(taskId: string) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [operations, setOperations] = useState<OperationSummary[]>([]);
  const [history, setHistory] = useState<HistoryMessage[]>([]);
  const [live, setLive] = useState<LiveItem[]>([]);
  const [historyError, setHistoryError] = useState<ApiError | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [sending, setSending] = useState(false);
  const [draftRevision, setDraftRevision] = useState(0);

  const loadHistory = useCallback(async () => {
    try {
      const loaded = await getHistory(taskId);
      setHistory(loaded.messages);
      setHistoryError(null);
      return true;
    } catch (failure) {
      setHistoryError(toApiError(failure));
      return false;
    }
  }, [taskId]);

  const loadOperations = useCallback(async () => {
    setOperations(await listOperations(taskId));
  }, [taskId]);

  const reload = useCallback(async () => {
    try {
      const [detail] = await Promise.all([getTask(taskId), loadOperations(), loadHistory()]);
      setTask(detail);
      setError(null);
    } catch (failure) {
      setError(toApiError(failure));
    }
  }, [taskId, loadOperations, loadHistory]);

  useEffect(() => {
    void reload();
  }, [reload]);

  /** 一轮调用结束：以服务端历史为准，丢掉已被历史覆盖的临时条目。 */
  const settle = useCallback(
    async (runId: string) => {
      const recorded = await loadHistory();
      void getTask(taskId).then(setTask).catch(() => undefined);
      setLive((items) =>
        items.filter((item) => item.keep || !recorded || item.runId !== runId),
      );
    },
    [loadHistory, taskId],
  );

  useEffect(() => {
    const onEvent = (event: AgentEvent) => {
      if (event.type === "session") {
        setTask((current) =>
          current === null ? current : { ...current, sdk_session_id: event.sdk_session_id },
        );
        return;
      }
      if (event.type === "text") {
        setLive((items) => {
          const index = items.findIndex(
            (item) => item.runId === event.run_id && item.kind === "message" && item.streaming,
          );
          if (index === -1) {
            return [
              ...items,
              {
                kind: "message",
                id: `live-${event.run_id}`,
                role: "assistant",
                text: event.text,
                streaming: true,
                runId: event.run_id,
                keep: false,
              },
            ];
          }
          const next = [...items];
          const target = next[index];
          if (target.kind !== "message") return items;
          next[index] = { ...target, text: target.text + event.text };
          return next;
        });
        return;
      }
      if (event.type === "draft_saved") {
        void loadOperations();
        setDraftRevision((value) => value + 1);
        return;
      }
      if (event.type === "done") {
        setLive((items) =>
          items.map((item) =>
            item.runId === event.run_id && item.kind === "message"
              ? { ...item, streaming: false }
              : item,
          ),
        );
        void settle(event.run_id);
        return;
      }
      setLive((items) => [
        ...items.map((item) =>
          item.runId === event.run_id && item.kind === "message"
            ? { ...item, streaming: false }
            : item,
        ),
        {
          kind: "failure",
          id: `error-${event.run_id}`,
          text: event.message,
          runId: event.run_id,
          keep: true,
        },
      ]);
      void settle(event.run_id);
    };

    return subscribeEvents(taskId, onEvent, () => void reload());
  }, [taskId, loadOperations, settle, reload]);

  const send = useCallback(
    async (message: string) => {
      setSending(true);
      try {
        const run = await sendMessage(taskId, message);
        setLive((items) => [
          ...items,
          {
            kind: "message",
            id: `sent-${run.run_id}`,
            role: "user",
            text: message,
            streaming: false,
            runId: run.run_id,
            keep: false,
          },
        ]);
        void getTask(taskId).then(setTask).catch(() => undefined);
        return null;
      } catch (failure) {
        return toApiError(failure);
      } finally {
        setSending(false);
      }
    },
    [taskId],
  );

  const feed: FeedItem[] = [
    ...history.map((message, index) => ({
      kind: "message" as const,
      id: `history-${index}`,
      role: message.role,
      text: message.text,
      streaming: false,
    })),
    ...live.map(({ runId: _runId, keep: _keep, ...item }) => item),
  ];

  return {
    task,
    operations,
    feed,
    historyError,
    error,
    sending,
    send,
    reload,
    draftRevision,
  };
}

export type OperationView = {
  summary: OperationSummary;
  draft: Draft | null;
  execution: Execution | null;
};

/** 操作的现时状态：执行视图是确认与执行的权威来源，摘要可能来自更早的一次读取。 */
export function effectiveStatus(view: OperationView): OperationStatus {
  return view.execution?.status ?? view.summary.status;
}

/**
 * 逐项读取草稿与执行结果。确认后的发送在后台线程执行，事件流不携带执行状态，
 * 因此存在 sending 时按固定间隔重读，直到状态离开 sending。
 */
export function useOperationViews(operations: OperationSummary[], revision: number) {
  const [views, setViews] = useState<OperationView[]>([]);
  const latest = useRef(operations);
  latest.current = operations;
  const key = operations.map((operation) => operation.operation_id).join("|");

  const reload = useCallback(async () => {
    const loaded = await Promise.all(
      latest.current.map(async (summary): Promise<OperationView> => {
        const [draft, execution] = await Promise.all([
          getDraft(summary.operation_id).catch(() => null),
          getExecution(summary.operation_id).catch(() => null),
        ]);
        return { summary, draft, execution };
      }),
    );
    setViews(loaded);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload, revision, key]);

  const executing = views.some((view) => effectiveStatus(view) === "sending");
  usePolling(() => void reload(), EXECUTION_POLL_MS, executing);

  return { views, reload };
}
