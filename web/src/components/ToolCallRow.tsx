import { useEffect, useRef, useState } from "react";
import { CaretDown, CaretUp, CheckCircle, CircleNotch, WarningCircle } from "@phosphor-icons/react";

import type { TimelineItem } from "../api";
import { writeClipboard } from "./MessageActions";
import { toolCallDisplay } from "./toolCallDisplay";

/** 未知工具沿用原始短参数；已识别的工具只显示用户能读懂的动作和对象。 */
const INLINE_VALUE_LIMIT = 60;

function inlineSummary(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(args)) {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    parts.push(text === undefined || text.length > INLINE_VALUE_LIMIT ? key : `${key}=${text}`);
  }
  return parts.join(", ");
}

function formatValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

const formatTime = (iso: string) =>
  new Date(iso).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });

const COPY_FEEDBACK_MS = 1600;

type Props = {
  item: Extract<TimelineItem, { kind: "tool" }>;
  active: boolean;
  focused: boolean;
  expanded: boolean;
  onToggle: () => void;
};

/**
 * 一次工具调用的轨迹行：收起时是系统细行（动作、对象、状态），展开后是
 * 完整参数与返回内容。定位（依据条目跳转、锚点高亮）由外层 mark 属性挂上。
 */
export default function ToolCallRow({ item, active, focused, expanded, onToggle }: Props) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const failed = item.status === "error";
  const unfinished = item.status === "running";
  const stateLabel = unfinished ? (active ? "进行中" : "未记录结果") : failed ? "失败" : "成功";
  const copy = async () => {
    setCopied(await writeClipboard(
      JSON.stringify(
        {
          tool_call_id: item.tool_call_id,
          name: item.name,
          arguments: item.arguments,
          status: item.status,
          result: item.result,
          created_at: item.created_at,
        },
        null,
        2,
      ),
    ));
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), COPY_FEEDBACK_MS);
  };
  const display = toolCallDisplay(item);
  const rowLabel = display.known && unfinished && !active
    ? display.label.replace(/^正在/, "") : display.label;
  const summary = display.known ? undefined : inlineSummary(item.arguments);
  return <div className="tool-call">
    <button type="button"
      className={`tool-call-row${failed ? " failed" : ""}`}
      onClick={onToggle}
      aria-expanded={expanded}
      aria-label={`${expanded ? "收起" : "展开"}${rowLabel}${display.target ? ` ${display.target}` : ""}${display.known && unfinished && !active ? ` ${stateLabel}` : ""}`}>
      {unfinished
        ? <CircleNotch size={14} className="tool-call-icon pending" aria-hidden />
        : failed
        ? <WarningCircle size={14} weight="fill" className="tool-call-icon" aria-hidden />
        : <CheckCircle size={14} weight="fill" className="tool-call-icon" aria-hidden />}
      <span className={`tool-call-name${display.known ? " readable" : ""}`}>{rowLabel}</span>
      {display.target && <span className="tool-call-args" title={display.target}>「{display.target}」</span>}
      {summary && <span className="tool-call-args">({summary})</span>}
      {!display.known && (failed || unfinished) && <span className={failed ? "tool-call-failed" : undefined}>{stateLabel}</span>}
      {display.known && unfinished && !active && <span>{stateLabel}</span>}
      <span className="rule" />
      {expanded
        ? <CaretUp size={12} weight="bold" aria-hidden />
        : <CaretDown size={12} weight="bold" aria-hidden />}
    </button>
    {expanded && <div className="tool-call-panel">
      <div className="tool-call-raw-name">工具：<code>{item.name}</code> · {stateLabel}</div>
      {Object.keys(item.arguments).length > 0 && <dl className="tool-call-fields">
        {Object.entries(item.arguments).map(([key, value]) => <div className="tool-call-field" key={key}>
          <dt>{key}</dt>
          <dd>{formatValue(value)}</dd>
        </div>)}
      </dl>}
      <div className="tool-call-result">
        <span className="tool-call-label">返回</span>
        <pre>{item.result ?? (active ? "工具仍在运行" : "中断时未记录结果")}</pre>
      </div>
      <div className="tool-call-meta">
        <time dateTime={item.created_at}>{formatTime(item.created_at)}</time>
        <span className="tool-call-id" title={item.tool_call_id}>
          tool_call_id {item.tool_call_id.slice(0, 6)}…
        </span>
        <button type="button" className="tool-call-copy" onClick={() => void copy()}>
          {copied ? "已复制" : "复制原始记录"}
        </button>
      </div>
    </div>}
    {focused && <span className="sr-only">定位到这次调用</span>}
  </div>;
}
