import { useEffect, useRef, type ReactNode } from "react";

import type { FeedItem } from "../hooks";

/**
 * 消息流：系统事件用小字嵌入，Agent 与用户消息分列，流式回复带输入指示。
 * 连续同一角色的消息成组，只有组内首条带头像，避免逐条重复身份标识。
 * Agent 消息是正文，与操作卡、结果卡同宽同边；用户消息是靠右的气泡。
 * 身份在视觉上由位置承担，读屏器另给一条隐藏说明。
 * 文本按原样展示（`white-space: pre-wrap`），不做 Markdown 渲染。
 */
export default function MessageFeed({ items, children }: { items: FeedItem[]; children?: ReactNode }) {
  const anchor = useRef<HTMLDivElement>(null);

  useEffect(() => {
    anchor.current?.scrollIntoView({ block: "end" });
  }, [items]);

  return (
    <>
      {items.map((item, index) => {
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
              <span style={{ color: "var(--color-danger)" }}>本轮处理失败：{item.text}</span>
              <span className="rule" />
            </div>
          );
        }
        const agent = item.role === "assistant";
        const previous = items[index - 1];
        const grouped = previous?.kind === "message" && previous.role === item.role;
        return (
          <div className={`msg ${agent ? "agent" : "user"}${grouped ? " cont" : ""}`} key={item.id}>
            {agent && (
              <div className="who" aria-hidden>
                A
              </div>
            )}
            <div className="msg-body">
              <span className="sr-only">{agent ? "Agent 说：" : "我说："}</span>
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
      {children !== undefined && <div className="flow-block">{children}</div>}
      <div ref={anchor} />
    </>
  );
}
