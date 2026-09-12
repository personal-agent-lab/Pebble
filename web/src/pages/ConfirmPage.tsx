import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError, confirmOperation, editDraft, type FieldError } from "../api";
import AppShell from "../components/AppShell";
import Notice from "../components/Notice";
import StatusBadge from "../components/StatusBadge";
import { effectiveStatus, useOperationViews, useTaskDetail, type OperationView } from "../hooks";
import { operationBadge, shortTime } from "../status";

const MAIL_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" />
    <polyline points="22,6 12,13 2,6" />
  </svg>
);

type Form = { to: string; subject: string; body: string };

function formOf(view: OperationView): Form {
  return {
    to: view.draft?.to.join(", ") ?? "",
    subject: view.draft?.subject ?? "",
    body: view.draft?.body ?? "",
  };
}

function fieldError(errors: FieldError[] | undefined, field: string): string | undefined {
  return errors?.find((error) => error.field === field)?.message;
}

type CardProps = {
  taskId: string;
  view: OperationView;
  onChanged: () => Promise<void>;
};

/**
 * 单个操作的编辑与确认。
 *
 * 编辑保存为新版本，确认绑定当前版本：修改后旧确认自动失效（后端返回版本冲突）。
 * 每个操作分别确认、分别执行，页面不提供批量确认。
 */
function OperationEditor({ taskId, view, onChanged }: CardProps) {
  const status = effectiveStatus(view);
  const version = view.execution?.version ?? view.summary.version;
  const editable = status === "pending";

  const [form, setForm] = useState<Form>(() => formOf(view));
  const [dirtyVersion, setDirtyVersion] = useState(version);
  const [busy, setBusy] = useState<"save" | "confirm" | null>(null);
  const [failure, setFailure] = useState<ApiError | null>(null);
  // 失败提示要说明「你依据的是哪一版」，而内容随即刷新到最新版，所以单独记住尝试的版本。
  const [attempted, setAttempted] = useState(version);

  // 版本变化（Agent 重写或自己保存）时以服务端内容为准，丢弃未保存的本地编辑。
  useEffect(() => {
    if (dirtyVersion !== version) {
      setForm(formOf(view));
      setDirtyVersion(version);
    }
  }, [version, dirtyVersion, view]);

  const recipients = form.to
    .split(/[,，;；\s]+/)
    .map((item) => item.trim())
    .filter((item) => item !== "");

  const save = async () => {
    setBusy("save");
    setFailure(null);
    setAttempted(version);
    try {
      await editDraft(view.summary.operation_id, version, {
        to: recipients,
        subject: form.subject,
        body: form.body,
      });
      await onChanged();
    } catch (error) {
      setFailure(error instanceof ApiError ? error : new ApiError("offline", String(error), 0));
    } finally {
      setBusy(null);
    }
  };

  const confirm = async () => {
    setBusy("confirm");
    setFailure(null);
    setAttempted(version);
    try {
      await confirmOperation(taskId, view.summary.operation_id, version);
      await onChanged();
    } catch (error) {
      setFailure(error instanceof ApiError ? error : new ApiError("offline", String(error), 0));
      await onChanged();
    } finally {
      setBusy(null);
    }
  };

  const result = view.execution?.result ?? null;

  return (
    <section className="op-card" data-component="MailDraftCard">
      <div className="op-head">
        <div className="op-kind">
          {MAIL_ICON}
          回复邮件草稿
        </div>
        <span className="op-ids">草稿 v{version}</span>
        <StatusBadge badge={operationBadge(status)} />
      </div>

      <div className="confirm-body">
        <div className="fields">
          <div className="field">
            <span className="k">to</span>
            <input
              className={`input${fieldError(failure?.fieldErrors, "to") ? " invalid" : ""}`}
              value={form.to}
              disabled={!editable}
              aria-label="收件人"
              onChange={(event) => setForm({ ...form, to: event.target.value })}
            />
            {fieldError(failure?.fieldErrors, "to") !== undefined && (
              <span className="field-error">{fieldError(failure?.fieldErrors, "to")}</span>
            )}
          </div>
          <div className="field">
            <span className="k">subject</span>
            <input
              className={`input${fieldError(failure?.fieldErrors, "subject") ? " invalid" : ""}`}
              value={form.subject}
              disabled={!editable}
              aria-label="主题"
              onChange={(event) => setForm({ ...form, subject: event.target.value })}
            />
            {fieldError(failure?.fieldErrors, "subject") !== undefined && (
              <span className="field-error">{fieldError(failure?.fieldErrors, "subject")}</span>
            )}
          </div>
          <div className="field">
            <span className="k">body</span>
            <textarea
              className={`input body${fieldError(failure?.fieldErrors, "body") ? " invalid" : ""}`}
              value={form.body}
              disabled={!editable}
              aria-label="正文"
              onChange={(event) => setForm({ ...form, body: event.target.value })}
            />
            {fieldError(failure?.fieldErrors, "body") !== undefined && (
              <span className="field-error">{fieldError(failure?.fieldErrors, "body")}</span>
            )}
          </div>
          <div className="field">
            <span className="k">thread</span>
            <span className="ro">
              {view.draft?.thread_id ?? "—"} · 原邮件 {view.draft?.source_message_id ?? "—"}
            </span>
          </div>
        </div>

        <aside className="side">
          <h5>副作用说明</h5>
          <div className="effect">
            确认后将以 <b>你的 Gmail 账号</b>向 <b>{recipients.join("、") || "上述收件人"}</b>{" "}
            发送上述完整内容。执行内容与所确认的 <b>v{version}</b> 逐字段一致，
            确认输入不含正文。
          </div>
          <div className="ver-list">
            <h5>版本历史</h5>
            {Array.from({ length: version }, (_, index) => index + 1).map((number) => (
              <div key={number} className={`ver${number === version ? " current" : ""}`}>
                <span className="id">v{number}</span>
                <span>{number === version ? "当前版本" : "历史版本"}</span>
              </div>
            ))}
          </div>
          {view.execution?.confirmation != null && (
            <div className="ver-list">
              <h5>确认记录</h5>
              <div className="ver">
                <span className="id">v{view.execution.confirmation.version}</span>
                <span>{shortTime(view.execution.confirmation.confirmed_at)} 确认</span>
              </div>
              {result !== null && (
                <div className="ver">
                  <span className="id">{result.status}</span>
                  <span>{result.status === "sent" ? result.message_id : result.reason}</span>
                </div>
              )}
            </div>
          )}
        </aside>
      </div>

      {failure !== null && (
        <div style={{ padding: "0 18px 14px" }}>
          <Notice
            tone={failure.unavailable ? "muted" : "danger"}
            title={
              failure.code === "version_conflict"
                ? "确认被拒绝：版本已更新"
                : failure.code === "invalid_draft"
                  ? "草稿未通过邮件校验"
                  : failure.code === "not_editable"
                    ? "当前状态不可编辑"
                    : failure.unavailable
                      ? "依赖尚未接入"
                      : "操作失败"
            }
          >
            {failure.code === "version_conflict" ? (
              <>
                你确认或编辑依据的是 <span className="mono">v{attempted}</span>，当前内容已是{" "}
                <span className="mono">v{failure.currentVersion}</span>
                。修改后旧确认不再适用，请查看最新版本后重新确认。该请求未产生任何发送调用。
              </>
            ) : failure.code === "invalid_draft" ? (
              "请按字段提示修改后重新保存。校验通过不表示已保存、已确认或已发送。"
            ) : (
              failure.message
            )}
          </Notice>
        </div>
      )}

      <div className="op-foot">
        <button type="button" className="btn" onClick={() => void confirm()} disabled={!editable || busy !== null}>
          {busy === "confirm" ? "确认中…" : `确认发送（v${version}）`}
        </button>
        <button
          type="button"
          className="btn-secondary"
          onClick={() => void save()}
          disabled={!editable || busy !== null}
        >
          {busy === "save" ? "保存中…" : "保存修改为新版本"}
        </button>
        <span className="note">
          {status === "pending" && "确认前仍可编辑；编辑保存后版本号更新，旧确认失效。"}
          {status === "sending" && "正在调用发送，字段已锁定。"}
          {status === "sent" && "已发送。重复确认返回已有状态，不会再次发送。"}
          {status === "failed" && "明确失败。不自动重试，也不沿用旧确认自动重发。"}
          {status === "unknown" && "结果待核实。未核实前不提供重发入口。"}
        </span>
      </div>
    </section>
  );
}

/** 草稿编辑与确认页。内容持久保存，关闭或刷新页面后仍可继续编辑与确认。 */
export default function ConfirmPage() {
  const { taskId = "" } = useParams();
  const navigate = useNavigate();
  const detail = useTaskDetail(taskId);
  const { views, reload: reloadViews } = useOperationViews(detail.operations, detail.draftRevision);

  const onChanged = async () => {
    await reloadViews();
    await detail.reload();
  };

  const pending = views.filter((view) => effectiveStatus(view) === "pending");

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

  return (
    <AppShell pendingCount={pending.length} serviceError={detail.error}>
      <div className="page">
        <div className="crumb">
          <Link to="/tasks">任务</Link>
          <span>/</span>
          <Link to={`/tasks/${taskId}`}>{detail.task?.goal ?? taskId.slice(0, 8)}</Link>
          <span>/</span>
          <span className="cur">待确认内容</span>
        </div>

        <div className="page-head">
          <div>
            <h2>草稿编辑与确认</h2>
            <div className="sub">
              关闭或刷新页面后，以下内容仍可继续编辑与确认；执行内容将与确认版本逐字段一致。
            </div>
          </div>
          <div className="flow-pill">
            准备 → 预览 → 编辑 → <b>确认</b> → 执行 → 结果
          </div>
        </div>

        {views.length === 0 && (
          <div className="list-card">
            <div className="empty">
              <div className="empty-title">没有待确认内容</div>
              <div className="empty-sub">
                Agent 准备好回复草稿后会保存到这里；回到任务对话继续处理。
              </div>
            </div>
          </div>
        )}

        {views.map((view) => (
          <OperationEditor
            key={view.summary.operation_id}
            taskId={taskId}
            view={view}
            onChanged={onChanged}
          />
        ))}
      </div>
    </AppShell>
  );
}
