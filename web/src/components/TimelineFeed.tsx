import { useEffect, useRef } from "react";

import { type ApiError, type MessageTarget, type TimelineItem } from "../api";
import MailDraftCard from "./MailDraftCard";
import Markdown from "./Markdown";

const STICK_PX = 48;
function scrollParent(node: HTMLElement | null): HTMLElement | null {
  for (let current = node?.parentElement ?? null; current !== null; current = current.parentElement) {
    const overflow = getComputedStyle(current).overflowY;
    if ((overflow === "auto" || overflow === "scroll") && current.scrollHeight > current.clientHeight) return current;
  }
  return (document.scrollingElement as HTMLElement | null) ?? document.documentElement;
}

type Props = {
  taskId: string;
  items: TimelineItem[];
  sendMessage: (message: string, target: MessageTarget) => Promise<ApiError | null>;
  onChanged: () => Promise<void>;
};

export default function TimelineFeed({ taskId, items, sendMessage, onChanged }: Props) {
  const anchor = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  useEffect(() => {
    const scroller = scrollParent(anchor.current);
    if (scroller === null) return;
    const onScroll = () => {
      stick.current = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= STICK_PX;
    };
    const target: EventTarget = scroller === document.scrollingElement || scroller === document.documentElement ? window : scroller;
    target.addEventListener("scroll", onScroll, { passive: true });
    return () => target.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => { if (stick.current) anchor.current?.scrollIntoView({ block: "end" }); });

  return <>
    {items.map((item, index) => {
      if (item.kind === "error") return <div className="sys-row" key={item.item_id}>
        <span className="error-text">本轮处理失败：{item.text}</span><span className="rule" />
      </div>;
      if (item.kind === "mail_draft") return <MailDraftCard key={item.item_id} taskId={taskId} item={item}
        sendMessage={sendMessage} onChanged={onChanged} />;
      const agent = item.role === "assistant";
      const previous = items[index - 1];
      const grouped = previous?.kind === "text" && previous.role === item.role;
      return <div className={`msg ${agent ? "agent" : "user"}${grouped ? " cont" : ""}`} key={item.item_id}>
        <div className="msg-body"><span className="sr-only">{agent ? "Agent 说：" : "我说："}</span>
          <div className="bubble">{agent ? <Markdown text={item.text} /> : item.text}</div>
          {item.attachments.length > 0 && <div className="message-attachments">
            {item.attachments.map((file) => <span className="attachment-chip" key={file.file_id}>{file.filename}</span>)}
          </div>}
        </div>
      </div>;
    })}
    <div ref={anchor} />
  </>;
}
