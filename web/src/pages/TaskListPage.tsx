import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, createTask, sendMessage } from "../api";
import AppShell from "../components/AppShell";
import Notice from "../components/Notice";
import StatusBadge from "../components/StatusBadge";
import { shortTime, taskBadge, taskHint } from "../status";
import { pendingTotal, useTasks } from "../tasks";

const TASK_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" />
    <polyline points="22,6 12,13 2,6" />
  </svg>
);

/**
 * 任务总览与新任务入口。自动触发的任务与手动发起的任务统一呈现，
 * 待确认任务最突出；列表按固定间隔轮询，新邮件到达后无需手动刷新。
 * 输入框固定在页面底部，列表再长也不必回到顶部才能发起任务。
 */
export default function TaskListPage() {
  const navigate = useNavigate();
  const { entries, error, reload } = useTasks();
  const [goal, setGoal] = useState("");
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<ApiError | null>(null);

  const pendingCount = pendingTotal(entries);

  const start = async () => {
    const text = goal.trim();
    if (text === "" || starting) return;
    setStarting(true);
    setStartError(null);
    try {
      const task = await createTask(text);
      // 目标同时作为首条消息交给 Agent；Agent 未接入时任务已建立，提交失败只提示。
      try {
        await sendMessage(task.task_id, text);
      } catch (failure) {
        if (!(failure instanceof ApiError) || !failure.unavailable) throw failure;
        setStartError(failure);
      }
      setGoal("");
      void reload();
      navigate(`/tasks/${task.task_id}`);
    } catch (failure) {
      setStartError(failure instanceof ApiError ? failure : new ApiError("offline", String(failure), 0));
    } finally {
      setStarting(false);
    }
  };

  return (
    <AppShell serviceError={error}>
      <div className="topbar">
        <h2>任务</h2>
        <span className="sub">
          {error !== null
            ? "未连接"
            : entries === null
              ? "读取中…"
              : `${entries.length} 个任务 · ${pendingCount} 项待确认`}
        </span>
      </div>

      <div className="content">
        {error !== null && (
          <div style={{ marginBottom: 16 }}>
            <Notice
              tone="danger"
              title="无法连接服务"
              actions={
                <button type="button" className="btn-secondary" onClick={() => void reload()}>
                  重试连接
                </button>
              }
            >
              {error.message}
              <br />
              确认 Pebble 服务已启动后重试。已保存的草稿与待确认内容不会丢失。
            </Notice>
          </div>
        )}

        {entries === null && error === null && <div className="loading">读取任务…</div>}

        {entries !== null && entries.length === 0 && (
          <div className="list-card">
            <div className="empty">
              <div className="empty-title">还没有任务</div>
              <div className="empty-sub">
                在输入框写下目标发起第一个任务；新邮件到达后会自动出现在这里，无需主动刷新。
              </div>
            </div>
          </div>
        )}

        {entries !== null && entries.length > 0 && (
          <div className="list-card">
            <div className="list-head">
              任务列表 · 按创建时间
              <span className="count">共 {entries.length} 个任务</span>
            </div>
            {entries.map((entry) => {
              const badge = taskBadge(entry.latestRun, entry.operations);
              return (
                <button
                  type="button"
                  key={entry.task.task_id}
                  className={`task-row${badge.tone === "wait" ? " hl" : ""}`}
                  onClick={() => navigate(`/tasks/${entry.task.task_id}`)}
                >
                  <div className="task-ic">{TASK_ICON}</div>
                  <div className="task-body">
                    <div className="task-title">{entry.task.goal}</div>
                    <div className="task-meta">{taskHint(entry.latestRun, entry.operations)}</div>
                  </div>
                  <div className="task-side">
                    <StatusBadge badge={badge} />
                    <span className="task-time">
                      {shortTime(entry.latestRun?.created_at ?? entry.task.created_at)}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="dock">
        <div className="dock-inner">
          {startError !== null && (
            <Notice
              tone={startError.unavailable ? "muted" : "danger"}
              title={startError.unavailable ? "Agent 暂未开放" : "发起失败"}
            >
              {startError.message}
              {startError.unavailable && "。任务已创建，开放后可继续。"}
            </Notice>
          )}

          <div className="composer">
            <input
              className="composer-input"
              value={goal}
              placeholder="输入目标开始任务…"
              aria-label="新任务目标"
              onChange={(event) => setGoal(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void start();
              }}
            />
            <button type="button" className="btn" onClick={() => void start()} disabled={goal.trim() === "" || starting}>
              {starting ? "发起中…" : "发起任务"}
            </button>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
