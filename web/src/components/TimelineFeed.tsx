import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { FileText } from "@phosphor-icons/react";

import { type ApiError, type Attachment, type MessageTarget, type TimelineItem } from "../api";
import { isMemoryNotice } from "../memory";
import MailDraftCard from "./MailDraftCard";
import Markdown from "./Markdown";
import MessageActions from "./MessageActions";

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

/** 连续几条 Agent 文字读起来是同一个回答，复制要拿到完整一段而不是最后一截。 */
function answerText(items: TimelineItem[], endIndex: number): string {
  const parts: string[] = [];
  for (let index = endIndex; index >= 0; index -= 1) {
    const item = items[index];
    if (item.kind !== "text" || item.role !== "assistant") break;
    parts.unshift(item.text);
  }
  return parts.join("\n\n");
}

type Props = {
  taskId: string;
  items: TimelineItem[];
  running: boolean;
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
  taskId, items, running, activity = null, focusItemId = null, retryRunId = null,
  retrying = false, retryMessage, sendMessage, onChanged,
}: Props) {
  const anchor = useRef<HTMLDivElement>(null);
  const stick = useRef(focusItemId === null);
  const focused = useRef<string | null>(null);
  const [highlight, setHighlight] = useState<string | null>(null);
  const [retryError, setRetryError] = useState<string | null>(null);
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
  const present = focusItemId !== null && items.some((item) => item.item_id === focusItemId);
  useEffect(() => {
    if (focusItemId === null || !present || focused.current === focusItemId) return;
    focused.current = focusItemId;
    stick.current = false;
    document.getElementById(itemAnchor(focusItemId))?.scrollIntoView({ block: "center" });
    setHighlight(focusItemId);
    const timer = window.setTimeout(() => setHighlight(null), FOCUS_MS);
    return () => window.clearTimeout(timer);
  }, [focusItemId, present]);
  const mark = (itemId: string) => ({
    id: itemAnchor(itemId),
    "data-focus": highlight === itemId ? "true" : undefined,
  });

  return <>
    {items.map((item, index) => {
      if (item.kind === "error") return <div className="sys-row" key={item.item_id} {...mark(item.item_id)}>
        <span className="error-text">本轮处理失败：{item.text}</span><span className="rule" />
      </div>;
      if (item.kind === "notice") return <div className="sys-row" key={item.item_id} {...mark(item.item_id)}>
        <span>{item.text}</span>
        {isMemoryNotice(item.text) && <Link className="sys-link" to="/memory">查看记忆</Link>}
        <span className="rule" />
      </div>;
      if (item.kind === "mail_draft") return <div className="focus-frame" key={item.item_id} {...mark(item.item_id)}>
        <MailDraftCard taskId={taskId} item={item} sendMessage={sendMessage} onChanged={onChanged} />
      </div>;
      const agent = item.role === "assistant";
      const previous = items[index - 1];
      const next = items[index + 1];
      const grouped = previous?.kind === "text" && previous.role === item.role;
      // 落款只挂在一个回答的最后一条上；还在流式输出时先不挂，
      // 否则复制到的是半截文字，时间也还不是这段回答的时间。
      const last = index === items.length - 1;
      const ended = !(next?.kind === "text" && next.role === "assistant") && !(last && running);
      const canRetry = !agent && item.run_id === retryRunId && retryMessage !== undefined;
      return <div className={`msg ${agent ? "agent" : "user"}${grouped ? " cont" : ""}`} key={item.item_id}
        {...mark(item.item_id)}>
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
          {agent && ended && <MessageActions text={answerText(items, index)}
            createdAt={item.created_at} pinned={last} />}
        </div>
      </div>;
    })}
    {running && <div className="thinking" role="status">
      {activity === null && <span className="sr-only">Agent 正在处理</span>}
      <span className="dot" aria-hidden /><span className="dot" aria-hidden /><span className="dot" aria-hidden />
      {activity !== null && <span className="thinking-text">{activity}</span>}
    </div>}
    <div ref={anchor} />
  </>;
}
