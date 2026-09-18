import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError, searchHistory, type HistoryHit, type HistorySpeaker } from "../api";
import AppShell from "../components/AppShell";
import Notice from "../components/Notice";
import { shortTime } from "../status";

const SEARCH_DELAY_MS = 300;

const SPEAKERS: Record<HistorySpeaker, string> = {
  user: "我",
  assistant: "Pebble",
  notice: "程序提示",
  mail_draft: "邮件草稿",
};

function hitLink(hit: HistoryHit): string {
  return `/tasks/${encodeURIComponent(hit.task_id)}#item-${encodeURIComponent(hit.item_id)}`;
}

/**
 * 历史对话搜索：跨任务查找过去说过的话、提示与邮件草稿，新的在前。
 * 搜索词写在地址里，从对话返回时结果还在；点开结果进入原对话并定位到命中位置。
 */
export default function SearchPage() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [text, setText] = useState(q);
  const [hits, setHits] = useState<HistoryHit[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    const trimmed = text.trim();
    const timer = window.setTimeout(() => {
      if (trimmed !== q) setParams(trimmed ? { q: trimmed } : {}, { replace: true });
    }, SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [text, q, setParams]);

  useEffect(() => {
    if (q === "") {
      setHits(null);
      setError(null);
      return;
    }
    let cancelled = false;
    searchHistory(q)
      .then((result) => { if (!cancelled) { setHits(result.results); setError(null); } })
      .catch((failure) => {
        if (!cancelled) setError(failure instanceof ApiError ? failure : new ApiError("invalid_request", String(failure), 0));
      });
    return () => { cancelled = true; };
  }, [q]);

  return (
    <AppShell serviceError={error}>
      <div className="topbar">
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2>搜索对话</h2>
        </div>
      </div>
      <div className="content kb-content">
        <input
          className="input kb-search"
          type="search"
          value={text}
          autoFocus
          placeholder="输入关键词，多个词用空格分隔…"
          aria-label="搜索对话"
          onChange={(event) => setText(event.target.value)}
        />

        {error !== null && (
          <Notice tone="danger" title="搜索失败">
            {error.fieldErrors?.map((item) => item.message).join("；") || error.message}
          </Notice>
        )}

        {q === "" && (
          <div className="empty">
            <div className="empty-sub">比如“预算评审”“方案 A”。结果按时间从新到旧排列，点开会跳到原对话。</div>
          </div>
        )}

        {q !== "" && hits !== null && hits.length === 0 && (
          <div className="empty"><div className="empty-sub">没有找到相关对话</div></div>
        )}

        {q !== "" && hits !== null && hits.length > 0 && (
          <div className="kb-hits">
            {hits.map((hit) => (
              <Link className="kb-hit" key={hit.item_id} to={hitLink(hit)}>
                <div className="history-hit-head">
                  <span className="kb-hit-title">{hit.task_title}</span>
                  <span className="history-hit-meta">{SPEAKERS[hit.speaker]} · {shortTime(hit.created_at)}</span>
                </div>
                <div className="kb-hit-snippet">{hit.snippet}</div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </AppShell>
  );
}
