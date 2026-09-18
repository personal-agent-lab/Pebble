import { useEffect, useRef, useState } from "react";

import { shortTime } from "../status";

const COPY_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
    strokeLinecap="round" strokeLinejoin="round">
    <rect x="9" y="9" width="13" height="13" rx="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </svg>
);

const DONE_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
    strokeLinecap="round" strokeLinejoin="round">
    <polyline points="20 6 9 17 4 12" />
  </svg>
);

const FEEDBACK_MS = 1600;

/**
 * 写剪贴板。异步剪贴板 API 只在安全上下文可用，而 Pebble 在局域网上是 http 访问
 * （README“远程访问”），那里 navigator.clipboard 直接不存在，必须退回 execCommand。
 */
async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard !== undefined) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* 落到下面的兜底路径 */ }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try { return document.execCommand("copy"); }
  catch { return false; }
  finally { area.remove(); }
}

type Props = {
  text: string;
  /** 不给就不显示时间：用户消息只挂复制按钮。 */
  createdAt?: string;
  pinned: boolean;
  /** 用户消息的落款贴在气泡右下角，按钮在最右，提示向左展开。 */
  end?: boolean;
  copyLabel?: string;
};

/**
 * 消息下方的落款：回答挂在左下角（复制整段回答与时间），用户消息挂在右下角（只复制）。
 *
 * pinned 是时间线最后一条——正在读的那条常驻显示，更早的消息靠悬停唤出，
 * 免得每段文字下面都挂一行灰字，把阅读栏切碎。
 */
export default function MessageActions({ text, createdAt, pinned, end = false, copyLabel = "复制回答" }: Props) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = async () => {
    setState(await writeClipboard(text) ? "copied" : "failed");
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), FEEDBACK_MS);
  };

  const label = state === "copied" ? "已复制" : state === "failed" ? "复制失败" : copyLabel;
  return <div className={`msg-actions${pinned ? " pinned" : ""}${end ? " end" : ""}`}>
    <button type="button" className="msg-action" onClick={() => void copy()} aria-label={label} title={label}>
      {state === "copied" ? DONE_ICON : COPY_ICON}
    </button>
    {createdAt !== undefined && <time className="msg-time" dateTime={createdAt}>{shortTime(createdAt)}</time>}
    <span className="msg-actions-note" role="status">{state === "idle" ? "" : label}</span>
  </div>;
}
