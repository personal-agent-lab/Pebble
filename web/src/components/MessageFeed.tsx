import { useEffect, useRef, type ReactNode } from "react";

import type { FeedItem } from "../hooks";

/**
 * 消息流：系统事件用等宽小字嵌入，Agent 与用户消息分列，流式回复带输入指示。
 * 文本按原样展示（`white-space: pre-wrap`），不做 Markdown 渲染。
 */
export default function MessageFeed({ items, children }: { items: FeedItem[]; children?: ReactNode }) {
  const anchor = useRef<HTMLDivElement>(null);

  useEffect(() => {
    anchor.current?.scrollIntoView({ block: "end" });
  }, [items]);

  return (
    <>
      {items.map((item) => {
        if (item.kind === "system") {
          return (
            <div className="sys-row" key={item.id}>
              <span>{item.text}</span>
              <span className="rule" />
            </div>
          );
        }
        if (item.kind === "failure") {
          return (
            <div className="sys-row" key={item.id}>
              <span style={{ color: "var(--color-danger)" }}>本轮调用失败：{item.text}</span>
              <span className="rule" />
            </div>
          );
        }
        const agent = item.role === "assistant";
        return (
          <div className={`msg ${agent ? "agent" : "user"}`} key={item.id}>
            <div className="who">{agent ? "A" : "我"}</div>
            <div className="msg-body">
              <div className="msg-label">{agent ? "agent" : "user"}</div>
              <div className="bubble">
                {item.text}
                {item.streaming && (
                  <span className="typing" aria-label="正在生成">
                    <span />
                    <span />
                    <span />
                  </span>
                )}
              </div>
            </div>
          </div>
        );
      })}
      {children}
      <div ref={anchor} />
    </>
  );
}
