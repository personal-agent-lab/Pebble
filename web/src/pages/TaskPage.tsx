import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError, confirmOperation, type Execution } from "../api";
import AppShell from "../components/AppShell";
import MessageFeed from "../components/MessageFeed";
import Notice from "../components/Notice";
import OperationCard from "../components/OperationCard";
import StatusBadge from "../components/StatusBadge";
import { effectiveStatus, useOperationViews, useTaskDetail, type OperationView } from "../hooks";
import { operationBadge, resultSummary, shortTime, taskBadge } from "../status";

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

/** 单项结果如实展示：sent 带外部标识，failed / unknown 带原因，unknown 不提供重发入口。 */
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
              已发送至 <b>{view.draft?.to.join("、") ?? "确认版本收件人"}</b>，内容与确认版本{" "}
              <b>v{execution.confirmation?.version}</b> 逐字段一致。邮件 ID{" "}
              <b className="mono">{result.message_id}</b>。
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
          {execution.confirmation !== null && (
            <span>
              确认 v{execution.confirmation.version} · {shortTime(execution.confirmation.confirmed_at)}
            </span>
          )}
          <span className="mono">{view.summary.operation_id.slice(0, 8)}</span>
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
    <AppShell pendingCount={pending.length} serviceError={detail.error}>
      <div className="chat-top">
        <button type="button" className="back" onClick={() => navigate("/tasks")} aria-label="返回任务列表">
          {BACK_ICON}
        </button>
        <div style={{ minWidth: 0, flex: 1 }}>
          <h2>{detail.task?.goal ?? "读取中…"}</h2>
          <div className="sub">
            {detail.task !== null && `开始 ${shortTime(detail.task.created_at)}`}
            {detail.task?.sdk_session_id != null && " · 已关联会话"}
          </div>
        </div>
        <StatusBadge badge={badge} />
      </div>

      <div className="chat-main">
        <div className="feed">
          {detail.historyError !== null && (
            <Notice
              tone="muted"
              title={detail.historyError.unavailable ? "Agent 尚未接入" : "历史读取失败"}
            >
              {detail.historyError.message}
              {detail.historyError.unavailable && "。本页仍展示本轮事件与已保存的待确认内容。"}
            </Notice>
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
                    ? "确认被拒绝：版本已更新"
                    : actionError.unavailable
                      ? "依赖尚未接入"
                      : "操作失败"
                }
              >
                {actionError.code === "version_conflict"
                  ? `当前内容已更新为 v${actionError.currentVersion}。修改后旧确认不再适用，请查看最新版本后重新确认；该请求未产生任何发送调用。`
                  : actionError.message}
              </Notice>
            )}

            {summary !== null && (
              <section className="results-section" data-component="ResultList">
                <div className="summary">
                  <span className="num mono">
                    {summary.done} / {summary.total}
                  </span>
                  <span className="txt">
                    <b>{summary.lead}</b>
                    {summary.detail}
                  </span>
                </div>
                <div className="results">
                  <div className="res-head">逐项结果 · 按确认时间</div>
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

        <aside className="ctx">
          <div className="ctx-sec">
            <h4>待确认操作</h4>
            {pending.length === 0 ? (
              <div className="ctx-empty">当前没有待确认内容。</div>
            ) : (
              pending.map((view) => (
                <Link
                  key={view.summary.operation_id}
                  className="ctx-op"
                  to={`/tasks/${taskId}/confirm`}
                  style={{ display: "block" }}
                >
                  <div className="t">
                    <span>回复邮件草稿</span>
                    <StatusBadge badge={{ tone: "wait", label: `v${view.execution?.version ?? view.summary.version}` }} />
                  </div>
                  <div className="d">编辑与确认</div>
                </Link>
              ))
            )}
          </div>
          <div className="ctx-sec secondary">
            <h4>任务信息</h4>
            <div className="kv">
              <span>任务</span>
              <span className="v">{taskId.slice(0, 8)}</span>
              <span>会话</span>
              <span className="v">{detail.task?.sdk_session_id ?? "尚未关联"}</span>
              <span>操作</span>
              <span className="v">{views.length}</span>
            </div>
          </div>
        </aside>
      </div>

      <div className="msg-composer">
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
    </AppShell>
  );
}
