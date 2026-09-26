import { useEffect, useId, useRef, useState } from "react";

import type { ContextCategory, ContextReading } from "../api";

/** 未知类别沿用原始标识，不猜语义。 */
const KIND_LABELS: Record<string, string> = {
  system_prompt: "系统提示词",
  system_tools: "工具定义",
  skills: "技能",
  messages: "对话消息",
  other: "其他",
  free_space: "可用空间",
};

export function contextCategoryLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

/** 小于 10% 时保留一位小数：0.4% 与 4% 在排查时是不同的量级。 */
export function formatPercentage(value: number | null | undefined): string {
  if (value === null || value === undefined) return "未记录";
  if (value > 0 && value < 10) return `${value.toFixed(1)}%`;
  return `${Math.round(value)}%`;
}

/**
 * 分解只取运行时给的正数类别；压缩预留不是已装内容，并入可用空间一起显示，
 * 全零说明这个运行时不报分解。
 */
function breakdown(categories: ContextCategory[] | undefined): ContextCategory[] {
  const positive = (categories ?? []).filter(
    (entry) => typeof entry.percentage === "number" && entry.percentage > 0,
  );
  const reserve = positive
    .filter((entry) => entry.kind === "auto_compact")
    .reduce((sum, entry) => sum + (entry.percentage as number), 0);
  const rest = positive.filter((entry) => entry.kind !== "auto_compact");
  if (reserve <= 0) return rest;
  const free = rest.find((entry) => entry.kind === "free_space");
  if (!free) return [...rest, { kind: "free_space", percentage: reserve }];
  return rest.map((entry) =>
    entry.kind === "free_space"
      ? { kind: entry.kind, percentage: (entry.percentage as number) + reserve }
      : entry,
  );
}

const SEGMENT_CLASS: Record<string, string> = {
  system_prompt: "ctx-seg-system",
  system_tools: "ctx-seg-tools",
  skills: "ctx-seg-skills",
  messages: "ctx-seg-messages",
  other: "ctx-seg-other",
  free_space: "ctx-seg-free_space",
};

const RING_SIZE = 16;
const RING_WIDTH = 2.5;
const RING_RADIUS = (RING_SIZE - RING_WIDTH) / 2;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

/** 占用环：走过的部分表示已用，读到 100% 就是一整圈。 */
function Ring({ percentage, nearLimit }: { percentage: number | null; nearLimit: boolean }) {
  const used = Math.min(100, Math.max(0, percentage ?? 0));
  return <svg className="context-ring" viewBox={`0 0 ${RING_SIZE} ${RING_SIZE}`}
    width={RING_SIZE} height={RING_SIZE} aria-hidden focusable="false">
    <circle className="context-ring-track" cx={RING_SIZE / 2} cy={RING_SIZE / 2}
      r={RING_RADIUS} strokeWidth={RING_WIDTH} fill="none" />
    <circle className={`context-ring-fill${nearLimit ? " near-limit" : ""}`}
      cx={RING_SIZE / 2} cy={RING_SIZE / 2} r={RING_RADIUS} strokeWidth={RING_WIDTH}
      fill="none" strokeLinecap="round"
      strokeDasharray={RING_CIRCUMFERENCE}
      strokeDashoffset={RING_CIRCUMFERENCE * (1 - used / 100)}
      transform={`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`} />
  </svg>;
}

type Props = {
  /** 最近一轮结束时的上下文读数；没有读数（还没跑过、或读取失败）时环是空的，提示写未记录。 */
  reading: ContextReading | null;
};

/**
 * 发送按钮左侧的上下文标记：一个占用环，点开是这个比例按类别的分解。
 *
 * 环上不带文字：读数只出现在悬停提示与无障碍名称里（有读数如「上下文已用 4.2%」，
 * 没有读数如「上下文已用 未记录」），免得常驻的一串数字和「未记录」占住输入框。
 *
 * 数据来自本地 CLI 的 `/context` 视图（Gateway 在轮末读取），运行时只给百分比、
 * 不给绝对 token 数，因此这里不折算 token。读数截至上一轮结束：正在跑的这一轮
 * 与还没发送的内容不在里面；没有分解数据的轮次面板里只有总量。
 */
export default function ContextMeter({ reading }: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const used = reading?.used_percentage ?? null;
  const categories = breakdown(reading?.categories);
  const threshold = reading?.threshold_percentage ?? null;
  const nearLimit = used !== null && threshold !== null && used >= threshold;
  const label = `上下文已用 ${formatPercentage(used)}`;

  return <div className="context-meter" ref={root}>
    <button ref={trigger} type="button" className="context-trigger" aria-label={label}
      aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? panelId : undefined}
      title={label} onClick={() => setOpen((value) => !value)}>
      <Ring percentage={used} nearLimit={nearLimit} />
    </button>

    {open && <div className="context-panel" id={panelId} role="dialog" aria-label="上下文占用">
      <div className="context-panel-head">
        <span className="context-panel-title">上下文已用</span>
        <strong className={nearLimit ? "near-limit" : undefined}>{formatPercentage(used)}</strong>
      </div>
      {categories.length > 0
        ? <ul className="context-legend">
          {categories.map((entry) => <li key={entry.kind}>
            <span className={`context-dot ${SEGMENT_CLASS[entry.kind] ?? "ctx-seg-other"}`} aria-hidden />
            <span className="context-legend-label">{contextCategoryLabel(entry.kind)}</span>
            <span className="context-legend-value">{formatPercentage(entry.percentage)}</span>
          </li>)}
        </ul>
        : <p className="context-panel-note">这个运行时不报上下文分解</p>}
    </div>}
  </div>;
}
