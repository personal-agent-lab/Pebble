/**
 * 侧栏“新结果”提醒的已读痕迹。
 *
 * 待确认是任务状态，处理完才消失，不需要已读痕迹；这里只记每个任务看过的最近一轮
 * 结束的调用：生成完还没打开看过的任务在侧栏标一个圆点。痕迹是这台设备上的查看记录，
 * 不是任务状态：服务端不保存，只落在 localStorage。
 */

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useLocation } from "react-router-dom";

import type { Run } from "./api";
import { useTasks } from "./tasks";

const RUNS_KEY = "pebble.tasks.seenRuns";
/** 早先按待确认记的已读痕迹，待确认改为处理完才消失后不再使用，读到就清掉。 */
const LEGACY_KEY = "pebble.tasks.seen";

/** 任务 ID → 读过的最近一轮结束调用的 run_id。 */
type RunMarks = Record<string, string>;

/** 最近一轮已经结束时给出它的 run_id；还在进行或从未调用过则没有可读的新结果。 */
function finishedRunId(run: Run | null): string | null {
  if (run === null || run.status === "pending" || run.status === "running") return null;
  return run.run_id;
}

/**
 * 从未记录过时返回 null：首次读到列表时把已有结果全部当作读过，
 * 免得刚启用这项提醒时历史任务一片圆点。本地存储不可用或内容损坏时同样处理。
 */
function readRunMarks(): RunMarks | null {
  try {
    window.localStorage.removeItem(LEGACY_KEY);
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

type SeenValue = {
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
  const [runMarks, setRunMarks] = useState<RunMarks | null>(readRunMarks);

  const opened = openTaskId(pathname);
  const entry = entries?.find((item) => item.task.task_id === opened) ?? null;
  // 停留在任务页期间结束的调用同样算读过：用户正看着它。
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

  // 首次读到列表时建立基线；之后任务删掉，痕迹跟着删，本地存储不留孤儿。
  useEffect(() => {
    if (entries === null) return;
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
      const alive = new Set(entries.map((item) => item.task.task_id));
      const kept = Object.entries(marked).filter(([taskId]) => alive.has(taskId));
      if (kept.length === Object.keys(marked).length) return marked;
      const next = Object.fromEntries(kept);
      writeRunMarks(next);
      return next;
    });
  }, [entries]);

  const value = useMemo<SeenValue>(() => ({
    fresh: (taskId, latestRun) => {
      const runId = finishedRunId(latestRun);
      return runMarks !== null && runId !== null && runMarks[taskId] !== runId;
    },
  }), [runMarks]);

  return <SeenContext.Provider value={value}>{children}</SeenContext.Provider>;
}

export function useSeen(): SeenValue {
  const value = useContext(SeenContext);
  if (value === null) throw new Error("useSeen 必须在 SeenProvider 内使用");
  return value;
}
