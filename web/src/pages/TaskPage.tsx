import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ApiError, confirmOperation, type Execution } from "../api";
import AppShell from "../components/AppShell";
import MessageFeed from "../components/MessageFeed";
import Notice from "../components/Notice";
import OperationCard from "../components/OperationCard";
import StatusBadge from "../components/StatusBadge";
import { effectiveStatus, useOperationViews, useTaskDetail, type OperationView } from "../hooks";
import { operationBadge, resultSummary, shortTime, taskBadge } from "../status";
import { useTasks } from "../tasks";

const BACK_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="15 18 9 12 15 6" />
  </svg>
);

const SEND_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <line x1="22" y1="2" x2="11" y2="13" />
    <polygon points="22 2 15 22 11 13 2 9 22 2" />
  </svg>
);

const OK_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="20 6 9 17 4 12" />
  </svg>
);

const ERR_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <line x1="18" y1="6" x2="6" y2="18" />
    <line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);

/** 单项结果如实展示：sent 说明发给了谁，failed / unknown 带原因，unknown 不提供重发入口。 */
function ResultRow({ view, execution }: { view: OperationView; execution: Execution }) {
  const result = execution.result;
  const status = effectiveStatus(view);
  const tone = result?.status === "sent" ? "ok" : result?.status === "failed" ? "err" : "";

  return (
    <div className="res-row">
      <div className={`res-ic ${tone}`}>{result?.status === "sent" ? OK_ICON : ERR_ICON}</div>
      <div className="res-body">
        <div className="res-title">回复邮件草稿</div>
        <div className="res-detail">
          {result === null && "已确认，正在执行。执行中内容不能原地修改。"}
          {result?.status === "sent" && (
            <>
              已发送至 <b>{view.draft?.to.join("、") ?? "已确认的收件人"}</b>，内容与你确认的逐字段一致。
            </>
          )}
          {result?.status === "failed" && (
            <>
              <b>发送失败：</b>
              {result.reason}。系统不会自动重试，也不会沿用旧确认自动重发。
            </>
          )}
          {result?.status === "unknown" && (
            <>
              <b>结果待核实：</b>
              {result.reason}。<b>未核实前不能再次发送</b>——查不到一次不等于未发送；重复确认只返回已有状态，不触发发送。
            </>
          )}
        </div>
        <div className="res-meta">
          {execution.confirmation !== null && <span>确认于 {shortTime(execution.confirmation.confirmed_at)}</span>}
        </div>
      </div>
      <div className="res-side">
        <StatusBadge badge={operationBadge(status)} />
      </div>
    </div>
  );
}

/**
 * 任务对话页：消息流、待确认内容与逐项执行结果。
 * 待确认卡与结果都读自已保存状态，关闭或刷新页面后原样恢复。
 */
export default function TaskPage() {
  const { taskId = "" } = useParams();
  const navigate = useNavigate();
  const detail = useTaskDetail(taskId);
  const { views, reload: reloadViews } = useOperationViews(detail.operations, detail.draftRevision);
  const tasks = useTasks();

  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const pending = views.filter((view) => effectiveStatus(view) === "pending");
  const executed = views.filter((view) => view.execution?.confirmation != null);

  const submit = async () => {
    const text = message.trim();
    if (text === "" || detail.sending) return;
    const failure = await detail.send(text);
    if (failure === null) {
      setMessage("");
      setActionError(null);
    } else {
      setActionError(failure);
    }
  };

  const confirm = async (view: OperationView) => {
    const version = view.execution?.version ?? view.summary.version;
    setConfirming(view.summary.operation_id);
    setActionError(null);
    try {
      await confirmOperation(taskId, view.summary.operation_id, version);
    } catch (failure) {
      setActionError(failure instanceof ApiError ? failure : new ApiError("offline", String(failure), 0));
    } finally {
      setConfirming(null);
      await reloadViews();
      await detail.reload();
      // 侧栏与入口的待确认计数读自任务列表，确认后立即重读，不等下一次轮询。
      void tasks.reload();
    }
  };

  if (detail.error !== null) {
    return (
      <AppShell serviceError={detail.error}>
        <div className="content">
          <Notice
            tone="danger"
            title={detail.error.httpStatus === 404 ? "任务不存在" : "无法连接服务"}
            actions={
              <button type="button" className="btn-secondary" onClick={() => navigate("/tasks")}>
                返回任务列表
              </button>
            }
          >
            {detail.error.message}
          </Notice>
        </div>
      </AppShell>
    );
  }

  const badge = taskBadge(detail.task?.latest_run ?? null, detail.operations);
  const summary = executed.length > 0 ? resultSummary(executed.map((view) => view.execution as Execution)) : null;

  return (
    <AppShell serviceError={detail.error}>
      <div className="chat-top">
        <button type="button" className="back" onClick={() => navigate("/tasks")} aria-label="返回任务列表">
          {BACK_ICON}
        </button>
        <div style={{ minWidth: 0, flex: 1 }}>
          <h2>{detail.task?.goal ?? "读取中…"}</h2>
          <div className="sub">{detail.task !== null && `开始 ${shortTime(detail.task.created_at)}`}</div>
        </div>
        <StatusBadge badge={badge} />
      </div>

      <div className="chat-main">
        <div className="feed">
          {detail.historyError !== null && (
            <div className="flow-block">
              <Notice tone="muted" title={detail.historyError.unavailable ? "Agent 暂未开放" : "历史读取失败"}>
                {detail.historyError.message}
                {detail.historyError.unavailable && "。本页仍展示本轮事件与已保存的待确认内容。"}
              </Notice>
            </div>
          )}

          <MessageFeed items={detail.feed}>
            {pending.map((view) => (
              <OperationCard
                key={view.summary.operation_id}
                taskId={taskId}
                view={view}
                confirming={confirming === view.summary.operation_id}
                onConfirm={() => void confirm(view)}
              />
            ))}

            {actionError !== null && (
              <Notice
                tone={actionError.unavailable ? "muted" : "danger"}
                title={
                  actionError.code === "version_conflict"
                    ? "确认被拒绝：内容已更新"
                    : actionError.unavailable
                      ? "功能暂未开放"
                      : "操作失败"
                }
              >
                {actionError.code === "version_conflict"
                  ? "草稿已被更新。内容变化后旧确认不再适用，请查看最新内容后重新确认；这次操作没有发出任何邮件。"
                  : actionError.message}
              </Notice>
            )}

            {summary !== null && (
              <section className="results-section" data-component="ResultList">
                <div className="results">
                  <div className="res-head">
                    执行结果 · 按确认时间
                    <span className="sum">
                      {summary.lead} · {summary.counts}
                    </span>
                  </div>
                  {summary.caveat !== null && <div className="res-note">{summary.caveat}</div>}
                  {executed.map((view) => (
                    <ResultRow
                      key={view.summary.operation_id}
                      view={view}
                      execution={view.execution as Execution}
                    />
                  ))}
                </div>
              </section>
            )}
          </MessageFeed>
        </div>
      </div>

      <div className="msg-composer">
        <div className="composer-row">
          <textarea
            value={message}
            placeholder="回复 Agent，或补充修改意见…"
            aria-label="消息"
            rows={1}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void submit();
              }
            }}
          />
          <button
            type="button"
            className="send-btn"
            onClick={() => void submit()}
            disabled={message.trim() === "" || detail.sending}
            aria-label="发送"
          >
            {SEND_ICON}
          </button>
        </div>
      </div>
    </AppShell>
  );
}
