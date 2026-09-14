import { useEffect, useRef, type ReactNode } from "react";

import type { FeedItem } from "../hooks";
import Markdown from "./Markdown";

/** 距底多少像素以内算"贴着底部"：容得下一次行高抖动，又不至于把明显上翻当成贴底。 */
const STICK_PX = 48;

/** 最近的可滚动祖先；窄屏下 .feed 是 overflow: visible，真正滚动的是外层或文档。 */
function scrollParent(node: HTMLElement | null): HTMLElement | null {
  for (let current = node?.parentElement ?? null; current !== null; current = current.parentElement) {
    const overflow = getComputedStyle(current).overflowY;
    if ((overflow === "auto" || overflow === "scroll") && current.scrollHeight > current.clientHeight) {
      return current;
    }
  }
  return (document.scrollingElement as HTMLElement | null) ?? document.documentElement;
}

function atBottom(scroller: HTMLElement): boolean {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= STICK_PX;
}

/**
 * 消息流：失败原因用小字嵌入，Agent 与用户消息分列，流式回复带输入指示。
 * 连续同一角色的消息成组，组内间距收紧。
 * Agent 消息是正文，通栏排版，与操作卡、结果卡同宽同边；用户消息是靠右的气泡。
 * 身份在视觉上由位置与底色承担，不摆头像；读屏器另给一条隐藏说明。
 * Agent 消息按 Markdown 渲染；用户消息按原样展示（`white-space: pre-wrap`）——
 * 那是用户自己敲进去的字，把它当标记解析会改写他们写下的内容。
 *
 * 自动滚动只在用户本来就停在底部时发生。feed 数组每次渲染都是新引用
 * （任务列表每 5 秒轮询、输入框打字都会触发重渲染），按引用做依赖会让页面
 * 周期性地把用户从任意位置拽回底部。
 */
export default function MessageFeed({ items, children }: { items: FeedItem[]; children?: ReactNode }) {
  const anchor = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    const scroller = scrollParent(anchor.current);
    if (scroller === null) return;
    const onScroll = () => {
      stick.current = atBottom(scroller);
    };
    // 文档级滚动的事件不在元素上派发，要听 window。
    const target: EventTarget =
      scroller === document.scrollingElement || scroller === document.documentElement ? window : scroller;
    target.addEventListener("scroll", onScroll, { passive: true });
    return () => target.removeEventListener("scroll", onScroll);
  }, []);

  // 不设依赖项：贴底与否是唯一条件。已经在底部时这次调用本就是空操作，
  // 用户上翻后则一律不动；消息、流式追加、异步出现的操作卡与结果卡一并覆盖。
  useEffect(() => {
    if (!stick.current) return;
    anchor.current?.scrollIntoView({ block: "end" });
  });

  return (
    <>
      {items.map((item, index) => {
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
            <div className="msg-body">
              <span className="sr-only">{agent ? "Agent 说：" : "我说："}</span>
              <div className="bubble">
                {agent ? <Markdown text={item.text} /> : item.text}
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
