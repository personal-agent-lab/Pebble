/**
 * 侧栏提醒的已读痕迹。
 *
 * “读过没有”是这台设备上的查看痕迹，不是任务状态：服务端不保存，只落在 localStorage。
 * 侧栏的圆点与计数据此只提醒没读过的待确认——读过不等于已处理，任务真实状态仍由
 * 任务页顶部的徽标给出。痕迹按操作版本记：草稿改出新版本就是新内容，要重新提醒。
 * 另记每个任务读过的最近一轮结束的调用：生成完还没打开看过的任务单独标一个圆点，不计数。
 */

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useLocation } from "react-router-dom";

import type { OperationSummary, Run } from "./api";
import type { TaskEntry } from "./hooks";
import { useTasks } from "./tasks";

const STORAGE_KEY = "pebble.tasks.seen";
const RUNS_KEY = "pebble.tasks.seenRuns";

/** 任务 ID → 读过的待确认标识；只留当下仍待确认的那些，确认完就随之消失。 */
type Marks = Record<string, string[]>;

/** 一条待确认的身份：同一操作改出新版本算新内容，版本进标识。 */
function pendingKeys(operations: OperationSummary[]): string[] {
  return operations
    .filter((operation) => operation.status === "pending")
    .map((operation) => `${operation.operation_id}:${operation.version}`);
}

/** 本地存储不可用或内容损坏时退回“什么都没读过”：至多多提醒一次，不影响使用。 */
function readMarks(): Marks {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw === null) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
    const marks: Marks = {};
    for (const [taskId, keys] of Object.entries(parsed as Record<string, unknown>)) {
      if (Array.isArray(keys) && keys.every((key) => typeof key === "string")) {
        marks[taskId] = keys as string[];
      }
    }
    return marks;
  } catch {
    return {};
  }
}

function writeMarks(marks: Marks): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(marks));
  } catch {
    /* 无痕模式等场景下写入被拒绝，忽略即可 */
  }
}

/** 任务 ID → 读过的最近一轮结束调用的 run_id。 */
type RunMarks = Record<string, string>;

/** 最近一轮已经结束时给出它的 run_id；还在进行或从未调用过则没有可读的新结果。 */
function finishedRunId(run: Run | null): string | null {
  if (run === null || run.status === "pending" || run.status === "running") return null;
  return run.run_id;
}

/**
 * 从未记录过时返回 null：首次读到列表时把已有结果全部当作读过，
 * 免得刚启用这项提醒时历史任务一片圆点。
 */
function readRunMarks(): RunMarks | null {
  try {
    const raw = window.localStorage.getItem(RUNS_KEY);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
    const marks: RunMarks = {};
    for (const [taskId, runId] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof runId === "string") marks[taskId] = runId;
    }
    return marks;
  } catch {
    return null;
  }
}

function writeRunMarks(marks: RunMarks): void {
  try {
    window.localStorage.setItem(RUNS_KEY, JSON.stringify(marks));
  } catch {
    /* 无痕模式等场景下写入被拒绝，忽略即可 */
  }
}

const sameKeys = (left: string[], right: string[]) =>
  left.length === right.length && left.every((key, index) => key === right[index]);

type SeenValue = {
  /** 该任务里没读过的待确认条数。 */
  unread: (taskId: string, operations: OperationSummary[]) => number;
  /** 全部任务里没读过的待确认条数；列表未读到时按 0 计，不显示占位数字。 */
  unreadTotal: (entries: TaskEntry[] | null) => number;
  /** 最近一轮调用已结束、结果还没打开看过。 */
  fresh: (taskId: string, latestRun: Run | null) => boolean;
};

const SeenContext = createContext<SeenValue | null>(null);

/** 正在看哪个任务由路由给出：/tasks/:taskId。 */
function openTaskId(pathname: string): string | null {
  const match = /^\/tasks\/([^/]+)/.exec(pathname);
  return match === null ? null : match[1];
}

export function SeenProvider({ children }: { children: ReactNode }) {
  const { entries } = useTasks();
  const { pathname } = useLocation();
  const [marks, setMarks] = useState<Marks>(readMarks);
  const [runMarks, setRunMarks] = useState<RunMarks | null>(readRunMarks);

  const opened = openTaskId(pathname);
  const entry = entries?.find((item) => item.task.task_id === opened) ?? null;
  // 停留在任务页期间到达的待确认同样算读过：用户正看着它。
  const keys = entry === null ? null : pendingKeys(entry.operations);
  const latest = useRef<string[]>([]);
  latest.current = keys ?? [];
  // 依赖要用标识本身，不能用数组：列表每轮轮询都换一个新数组，按引用比会无限触发。
  const signature = keys?.join(",") ?? null;

  useEffect(() => {
    if (opened === null || signature === null) return;
    const current = latest.current;
    setMarks((marked) => {
      if (sameKeys(marked[opened] ?? [], current)) return marked;
      const next = { ...marked, [opened]: current };
      writeMarks(next);
      return next;
    });
  }, [opened, signature]);

  // 停留在任务页期间结束的调用同样算读过。
  const openedRun = entry === null ? null : finishedRunId(entry.latestRun);
  useEffect(() => {
    if (opened === null || openedRun === null) return;
    setRunMarks((marked) => {
      if (marked === null || marked[opened] === openedRun) return marked;
      const next = { ...marked, [opened]: openedRun };
      writeRunMarks(next);
      return next;
    });
  }, [opened, openedRun]);

  // 任务删掉后痕迹跟着删，本地存储不留孤儿。
  useEffect(() => {
    if (entries === null) return;
    const alive = new Set(entries.map((item) => item.task.task_id));
    setMarks((marked) => {
      const kept = Object.entries(marked).filter(([taskId]) => alive.has(taskId));
      if (kept.length === Object.keys(marked).length) return marked;
      const next = Object.fromEntries(kept);
      writeMarks(next);
      return next;
    });
    setRunMarks((marked) => {
      if (marked === null) {
        const baseline: RunMarks = {};
        for (const item of entries) {
          const runId = finishedRunId(item.latestRun);
          if (runId !== null) baseline[item.task.task_id] = runId;
        }
        writeRunMarks(baseline);
        return baseline;
      }
      const kept = Object.entries(marked).filter(([taskId]) => alive.has(taskId));
      if (kept.length === Object.keys(marked).length) return marked;
      const next = Object.fromEntries(kept);
      writeRunMarks(next);
      return next;
    });
  }, [entries]);

  const value = useMemo<SeenValue>(() => {
    const unread = (taskId: string, operations: OperationSummary[]) => {
      const seen = marks[taskId] ?? [];
      return pendingKeys(operations).filter((key) => !seen.includes(key)).length;
    };
    return {
      unread,
      unreadTotal: (list) =>
        list?.reduce((total, item) => total + unread(item.task.task_id, item.operations), 0) ?? 0,
      fresh: (taskId, latestRun) => {
        const runId = finishedRunId(latestRun);
        return runMarks !== null && runId !== null && runMarks[taskId] !== runId;
      },
    };
  }, [marks, runMarks]);

  return <SeenContext.Provider value={value}>{children}</SeenContext.Provider>;
}

export function useSeen(): SeenValue {
  const value = useContext(SeenContext);
  if (value === null) throw new Error("useSeen 必须在 SeenProvider 内使用");
  return value;
}
