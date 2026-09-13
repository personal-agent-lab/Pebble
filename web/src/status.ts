/**
 * 任务与操作状态推导（纯函数）。
 *
 * 后端不保存任务级状态：任务的展示状态由 latest_run 的调用状态与该任务全部操作状态共同
 * 推导，两者含义不同（契约第 9 节），这里只做展示层归并，不写回后端。
 */

import type { Execution, OperationStatus, OperationSummary, Run } from "./api";

export type BadgeTone = "neutral" | "wait" | "run" | "ok" | "err" | "unk";

export type Badge = { tone: BadgeTone; label: string };

export const OPERATION_BADGES: Record<OperationStatus, Badge> = {
  pending: { tone: "wait", label: "待确认" },
  sending: { tone: "run", label: "执行中" },
  sent: { tone: "ok", label: "已发送" },
  failed: { tone: "err", label: "已失败" },
  unknown: { tone: "unk", label: "待核实" },
};

export function operationBadge(status: OperationStatus): Badge {
  return OPERATION_BADGES[status];
}

/**
 * 任务徽标。等待用户确认最优先——这是唯一需要用户动作的状态；随后是调用进行中，
 * 再按操作结果的确定性从低到高退让，全部确定且成功才是 done。
 */
export function taskBadge(latestRun: Run | null, operations: OperationSummary[]): Badge {
  const pending = operations.filter((operation) => operation.status === "pending").length;
  if (pending > 0) return { tone: "wait", label: `${pending} 项待确认` };

  if (latestRun !== null && (latestRun.status === "pending" || latestRun.status === "running")) {
    return { tone: "run", label: "处理中" };
  }
  if (operations.some((operation) => operation.status === "sending")) {
    return { tone: "run", label: "执行中" };
  }
  if (operations.some((operation) => operation.status === "unknown")) {
    return { tone: "unk", label: "待核实" };
  }
  if (operations.some((operation) => operation.status === "failed")) {
    return { tone: "err", label: "已失败" };
  }
  if (latestRun !== null && latestRun.status === "error") {
    return { tone: "err", label: "处理失败" };
  }
  if (latestRun !== null && latestRun.status === "interrupted") {
    return { tone: "unk", label: "已中断" };
  }
  if (latestRun === null && operations.length === 0) {
    return { tone: "neutral", label: "待开始" };
  }
  return { tone: "ok", label: "已完成" };
}

/** 任务行的补充说明，只用已读到的数据，不猜测 Agent 进展。 */
export function taskHint(latestRun: Run | null, operations: OperationSummary[]): string {
  const pending = operations.filter((operation) => operation.status === "pending").length;
  if (pending > 0) return `${pending} 项待确认内容已保存，关闭页面后仍可继续编辑`;
  if (latestRun?.status === "running") return "Agent 正在处理";
  if (latestRun?.status === "pending") return "已接受输入，等待开始";
  if (latestRun?.error) return latestRun.error;
  if (operations.some((operation) => operation.status === "sending")) return "已确认，正在执行";
  if (operations.some((operation) => operation.status === "unknown")) {
    return "发送结果待核实，未核实前不会再次发送";
  }
  if (operations.length > 0) {
    const sent = operations.filter((operation) => operation.status === "sent").length;
    return `${operations.length} 项操作，${sent} 项已发送`;
  }
  return "尚无操作";
}

/**
 * 逐项结果汇总；部分成功如实写成 N / M，不报告为整体成功。
 *
 * 汇总是结果卡的一行注记，不是独立版块：全部成功时没有需要提醒的分歧，
 * caveat 留空；有失败或待核实才说明各项互不影响。
 */
export function resultSummary(
  executions: Execution[],
): { lead: string; counts: string; caveat: string | null } {
  const total = executions.length;
  const done = executions.filter((execution) => execution.result?.status === "sent").length;
  const failed = executions.filter((execution) => execution.result?.status === "failed").length;
  const unknown = executions.filter((execution) => execution.result?.status === "unknown").length;

  const rest: string[] = [];
  if (failed > 0) rest.push(`${failed} 项失败`);
  if (unknown > 0) rest.push(`${unknown} 项待核实`);

  return {
    lead: done === total ? "全部完成" : "部分完成",
    counts: [`${done} / ${total} 已发送`, ...rest].join("、"),
    caveat:
      done === total ? null : "各项结果独立记录，失败或待核实不影响已成功项。",
  };
}

const TIME = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
const DATE_TIME = new Intl.DateTimeFormat("zh-CN", {
  month: "numeric",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** 当天只显示时分，其余带日期；后端时间为带时区的 ISO 8601。 */
export function shortTime(iso: string | null): string {
  if (iso === null) return "";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return iso;
  const now = new Date();
  const sameDay =
    value.getFullYear() === now.getFullYear() &&
    value.getMonth() === now.getMonth() &&
    value.getDate() === now.getDate();
  return sameDay ? TIME.format(value) : DATE_TIME.format(value);
}
