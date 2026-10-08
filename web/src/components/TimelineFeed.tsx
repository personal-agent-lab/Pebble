import { Fragment, useEffect, useRef, useState } from "react";
import FailureDetails from "./FailureDetails";
import { Link } from "react-router-dom";

import { FileText } from "@phosphor-icons/react";

import {
  type ApiError,
  type Attachment,
  type MessageTarget,
  type RunObservation,
  type TimelineItem,
} from "../api";
import { isMemoryNotice } from "../memory";
import MailDraftCard from "./MailDraftCard";
import Markdown from "./Markdown";
import MessageActions from "./MessageActions";
import RunDuration from "./RunDuration";
import ToolCallRow from "./ToolCallRow";

const STICK_PX = 48;
const FOCUS_MS = 2400;

/** 时间线条目的页面锚点：历史搜索结果链接到 `/tasks/:id#item-<item_id>`。 */
export const itemAnchor = (itemId: string) => `item-${itemId}`;

/**
 * 消息流的滚动容器：桌面是 .feed，手机折叠成整页滚动。
 *
 * 这里只认 overflow，不再要求“此刻已经溢出”：挂载时时间线还是空的，
 * .feed 没溢出就会被跳过，误把整个文档当成滚动容器。文档在桌面布局下
 * 被 .app 的 overflow:hidden 锁死、永远不发 scroll 事件，贴底判断也就
 * 永远停在初始的 true，于是每次重渲染都把视图拽回底部。
 */
function scrollParent(node: HTMLElement | null): HTMLElement {
  for (let current = node?.parentElement ?? null; current !== null; current = current.parentElement) {
    const overflow = getComputedStyle(current).overflowY;
    if (overflow === "auto" || overflow === "scroll") return current;
  }
  return (document.scrollingElement as HTMLElement | null) ?? document.documentElement;
}

/** 文档滚动只在 window 上派发，元素滚动在元素自己身上。 */
const scrollEventTarget = (scroller: HTMLElement): EventTarget =>
  scroller === document.scrollingElement || scroller === document.documentElement ? window : scroller;

/** 时间线内容是否有新东西：条数、末条身份，以及末条自身的增长（流式文字与草稿改版）。 */
function contentSignature(items: TimelineItem[]): string {
  const last = items[items.length - 1];
  if (last === undefined) return "0";
  const growth = last.kind === "text" ? `${last.text.length}:${(last.attachments ?? []).map((item) => item.file_id).join(",")}`
    : last.kind === "mail_draft" ? `${last.draft.version}:${last.execution.status}`
    : last.kind === "tool" ? `${last.name}:${last.status}`
    : last.text.length;
  return `${items.length}:${last.item_id}:${growth}`;
}

const formatSize = (size: number) => size >= 1024 * 1024
  ? `${(size / 1024 / 1024).toFixed(1)} MB`
  : `${Math.max(1, Math.round(size / 1024))} KB`;

function MessageAttachments({ items }: { items: Attachment[] }) {
  if (items.length === 0) return null;
  return <div className="message-attachments">
    {items.map((attachment) => attachment.mime_type.startsWith("image/")
      ? <a className="message-image" href={attachment.url} target="_blank" rel="noreferrer"
          key={attachment.file_id} aria-label={`查看图片 ${attachment.filename}`}>
          <img src={attachment.url} alt={attachment.filename} />
        </a>
      : <a className="message-file" href={attachment.url} key={attachment.file_id} download>
          <FileText size={22} weight="regular" />
          <span><strong>{attachment.filename}</strong><small>{formatSize(attachment.size)}</small></span>
        </a>)}
  </div>;
}

/** 一轮回答可能被工具行分隔；复制时取本轮全部助手文字，不含过程叙述。 */
function answerText(items: TimelineItem[], runId: string, narrationIds: Set<string>): string {
  const parts: string[] = [];
  for (const item of items) {
    if (item.kind === "text" && item.role === "assistant" && item.run_id === runId
      && !narrationIds.has(item.item_id)) {
      parts.push(item.text);
    }
  }
  return parts.join("\n\n");
}

/** 一段连续的同轮条目；index 是条目在完整时间线里的位置。 */
type RunSegment = {
  runId: string;
  parts: { item: TimelineItem; index: number; process: boolean }[];
};

/**
 * 连续的同轮条目合成一段。工具行都是过程；助手文字位于本段最后一次工具
 * 调用之前的也是过程（叙述）。其余是结果：用户消息、草稿卡片、最终回答、
 * 提示与错误。
 *
 * 过程条目在展开的折叠分组里按过程样式渲染（小一号、次要色、行距收到工具行
 * 一档），与工具行同属一段经过；`.process` 就是这件事的标记，既决定样式，也
 * 决定收起分组时哪些条目留在外面，本轮进行中时同样把叙述与工具行压成一档。
 */
function segmentsOf(items: TimelineItem[]): RunSegment[] {
  const segments: RunSegment[] = [];
  items.forEach((item, index) => {
    const last = segments[segments.length - 1];
    if (last && last.runId === item.run_id) last.parts.push({ item, index, process: false });
    else segments.push({ runId: item.run_id, parts: [{ item, index, process: false }] });
  });
  for (const seg of segments) {
    let lastTool = -1;
    seg.parts.forEach((part, i) => { if (part.item.kind === "tool") lastTool = i; });
    seg.parts.forEach((part, i) => {
      part.process = part.item.kind === "tool"
        || (i < lastTool && part.item.kind === "text" && part.item.role === "assistant");
    });
  }
  return segments;
}

type Props = {
  taskId: string;
  items: TimelineItem[];
  /** 每轮的运行观测：折叠头取时长，上下文标记取轮末读数。 */
  observations?: RunObservation[];
  running: boolean;
  activeRunId?: string | null;
  /** 进行中的当前步骤说明；没有时只显示跳动的点。 */
  activity?: string | null;
  /** 从历史搜索跳转过来时要定位的条目：滚到它并短暂高亮，不再自动贴底。 */
  focusItemId?: string | null;
  /** 只有任务最后一轮是已中断的用户消息时才有值。 */
  retryRunId?: string | null;
  retrying?: boolean;
  retryMessage?: () => Promise<ApiError | null>;
  sendMessage: (message: string, target: MessageTarget) => Promise<ApiError | null>;
  onChanged: () => Promise<void>;
};

export default function TimelineFeed({
  taskId, items, observations = [], running, activeRunId = null, activity = null, focusItemId = null, retryRunId = null,
  retrying = false, retryMessage, sendMessage, onChanged,
}: Props) {
  const anchor = useRef<HTMLDivElement>(null);
  const stick = useRef(focusItemId === null);
  const focused = useRef<string | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set());
  const [expandedRuns, setExpandedRuns] = useState<Set<string>>(new Set());
  const toggleTool = (itemId: string) => {
    setExpandedTools((prev) => {
      const next = new Set(prev);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  };
  const toggleRunDuration = (runId: string) => {
    setExpandedRuns((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  };
  useEffect(() => {
    let scroller = scrollParent(anchor.current);
    let target = scrollEventTarget(scroller);
    const onScroll = () => {
      stick.current = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= STICK_PX;
    };
    // 跨过 900px 断点时滚动容器会在 .feed 与文档之间换人，换了就重新挂监听。
    const rebind = () => {
      const next = scrollParent(anchor.current);
      if (next === scroller) return;
      target.removeEventListener("scroll", onScroll);
      scroller = next;
      target = scrollEventTarget(scroller);
      target.addEventListener("scroll", onScroll, { passive: true });
    };
    target.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", rebind);
    return () => {
      target.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", rebind);
    };
  }, []);

  // 只在时间线真的有新内容时贴底。不加依赖会让每一次重渲染都滚动：
  // 任务列表 5 秒一轮的轮询、输入框里敲的每一个字，都会把页面拽到底部。
  // running 一并入依赖：思考占位出入会改变内容高度，和新增一条消息一样需要贴底。
  const signature = contentSignature(items);
  useEffect(() => { if (stick.current) anchor.current?.scrollIntoView({ block: "end" }); }, [signature, running]);

  // 定位只做一次：条目读出来之后滚到它；之后的新内容照常，不再把视图拽回这里。
  // 定位到工具行时顺带展开；命中被折叠的过程条目时先展开所在轮的分组再滚动。
  const present = focusItemId !== null && items.some((item) => item.item_id === focusItemId);
  useEffect(() => {
    if (focusItemId === null || !present || focused.current === focusItemId) return;
    focused.current = focusItemId;
    stick.current = false;
    const target = items.find((item) => item.item_id === focusItemId);
    // 命中被折叠的过程条目（工具行或叙述文字）时，先展开所在轮的分组再滚动。
    const folded = target !== undefined && target.run_id !== currentRunId
      && (target.kind === "tool" || narrationIds.has(target.item_id));
    if (folded && target !== undefined) {
      setExpandedRuns((prev) => new Set(prev).add(target.run_id));
    }
    if (target?.kind === "tool") {
      setExpandedTools((prev) => new Set(prev).add(focusItemId));
    }
    const scroll = () => document.getElementById(itemAnchor(focusItemId))?.scrollIntoView({ block: "center" });
    if (folded) requestAnimationFrame(scroll); else scroll();
    setHighlight(focusItemId);
    const timer = window.setTimeout(() => setHighlight(null), FOCUS_MS);
    return () => window.clearTimeout(timer);
  }, [focusItemId, present]);
  const mark = (itemId: string) => ({
    id: itemAnchor(itemId),
    "data-focus": highlight === itemId ? "true" : undefined,
  });
  const lastAssistantByRun = new Map<string, string>();
  for (const item of items) {
    if (item.kind === "text" && item.role === "assistant") lastAssistantByRun.set(item.run_id, item.item_id);
  }
  const currentRunId = running ? activeRunId ?? items[items.length - 1]?.run_id : null;
  const observationByRun = new Map(observations.map((run) => [run.run_id, run]));
  const segments = segmentsOf(items);
  // 被折叠起来的叙述文字：复制回答时不把它们混进正文。
  const narrationIds = new Set<string>();
  for (const seg of segments) {
    for (const { item, process } of seg.parts) {
      if (process && item.kind === "text") narrationIds.add(item.item_id);
    }
  }

  // 单个条目按原有形态渲染；哪些条目出现、分组头插在哪里，由 renderSegment 决定。
  const renderItem = (item: TimelineItem, index: number, process = false) => {
    if (item.kind === "error") return <div key={item.item_id}>
      <div className="sys-row" {...mark(item.item_id)}>
        <span className="error-text">{item.failure ? item.failure.message : `本轮处理失败：${item.text}`}</span><span className="rule" />
      </div>
      {item.failure && <FailureDetails failure={item.failure} />}
    </div>;
    if (item.kind === "notice") return <div key={item.item_id}>
      <div className="sys-row" {...mark(item.item_id)}>
        <span>{item.text}</span>
        {isMemoryNotice(item.text) && <Link className="sys-link" to="/memory">查看记忆</Link>}
      </div>
    </div>;
    if (item.kind === "mail_draft") return <div key={item.item_id}>
      <div className="focus-frame" {...mark(item.item_id)}>
        <MailDraftCard taskId={taskId} item={item} sendMessage={sendMessage} onChanged={onChanged} />
      </div>
    </div>;
    if (item.kind === "tool") return <div key={item.item_id}
      className={`tool-item${process ? " process" : ""}`}>
      <div {...mark(item.item_id)}>
        <ToolCallRow item={item} focused={highlight === item.item_id}
          active={running && item.run_id === activeRunId}
          expanded={expandedTools.has(item.item_id)}
          onToggle={() => toggleTool(item.item_id)} />
      </div>
    </div>;
    const agent = item.role === "assistant";
    const previous = items[index - 1];
    const grouped = previous?.kind === "text" && previous.role === item.role;
    // 同一轮即使被工具调用分成多段，也只在最后一段挂一次复制入口。
    const last = index === items.length - 1;
    const ended = agent && lastAssistantByRun.get(item.run_id) === item.item_id
      && item.run_id !== currentRunId;
    const canRetry = !agent && item.run_id === retryRunId && retryMessage !== undefined;
    // 过程标记同时挂在外层包装上：内层是消息本身（字号与颜色的作用点），外层与
    // 工具行同级，相邻行距按它匹配；只标在内层就对不上兄弟条目。
    return <div key={item.item_id} className={process ? "process" : undefined} {...mark(item.item_id)}>
      <div className={`msg ${agent ? "agent" : "user"}${grouped ? " cont" : ""}${process ? " process" : ""}`}>
        <div className="msg-body"><span className="sr-only">{agent ? "Agent 说：" : "我说："}</span>
          {item.text && <div className="bubble">{agent ? <Markdown text={item.text} /> : item.text}</div>}
          {!agent && <MessageAttachments items={item.attachments ?? []} />}
          {!agent && (canRetry || item.text) && <div className="user-message-actions">
            {canRetry && retryError !== null && <span className="retry-error" role="status">{retryError}</span>}
            {canRetry && <button type="button" className="retry-message" disabled={retrying} onClick={() => {
              setRetryError(null);
              void retryMessage().then((error) => setRetryError(error?.message ?? null));
            }} aria-label={retrying ? "正在重试这条消息" : "重试这条消息"} title="重试">
              <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
                strokeLinecap="round" strokeLinejoin="round" aria-hidden>
                <path d="M20 11a8 8 0 1 0-2.34 5.66" /><polyline points="20 4 20 11 13 11" />
              </svg>
            </button>}
            {item.text && <MessageActions text={item.text} pinned={false} end copyLabel="复制消息" />}
          </div>}
          {ended && <MessageActions text={answerText(items, item.run_id, narrationIds)}
            createdAt={item.created_at} pinned={last} />}
        </div>
      </div>
    </div>;
  };

  const renderSegment = (seg: RunSegment) => {
    // 这一轮的运行观测只用来给折叠头取时长；材料与用量不在界面上展示。
    const observed = observationByRun.get(seg.runId);
    const active = seg.runId === currentRunId;
    const firstProcess = seg.parts.findIndex((part) => part.process);
    // 没有过程要折叠，或这一轮正在进行：条目按时间顺序逐条显示，实时进展不折叠。
    if (firstProcess === -1 || active) {
      return <Fragment key={seg.parts[0].item.item_id}>
        {seg.parts.map(({ item, index, process }) => renderItem(item, index, process))}
      </Fragment>;
    }
    // 已结束且有过程：折叠成一组，收起只留结果；头部显示用时，展开按时间顺序还原过程。
    const expanded = expandedRuns.has(seg.runId);
    const header = <RunDuration run={observed} expanded={expanded}
      onToggle={() => toggleRunDuration(seg.runId)} />;
    return <Fragment key={seg.parts[0].item.item_id}>
      {seg.parts.map(({ item, process, index }, i) => {
        // 组头插在第一条过程条目的位置上：折叠时它替掉整段过程，展开时过程照常显示。
        if (i !== firstProcess) {
          return process ? (expanded ? renderItem(item, index, true) : null) : renderItem(item, index);
        }
        if (!process) return renderItem(item, index);
        return <Fragment key={`${item.item_id}:header`}>
          {header}{expanded ? renderItem(item, index, true) : null}
        </Fragment>;
      })}
    </Fragment>;
  };

  return <>
    {segments.map(renderSegment)}
    {running && <div className="thinking" role="status">
      {activity === null && <span className="sr-only">Agent 正在处理</span>}
      <span className="dot" aria-hidden /><span className="dot" aria-hidden /><span className="dot" aria-hidden />
      {activity !== null && <span className="thinking-text">{activity}</span>}
    </div>}
    <div ref={anchor} />
  </>;
}
