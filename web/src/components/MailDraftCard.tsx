import { useEffect, useState } from "react";

import {
  ApiError,
  confirmOperation,
  editDraft,
  type MessageTarget,
  type TimelineItem,
  type UploadedFile,
  uploadFile,
  verifyExecution,
} from "../api";
import { operationBadge, shortTime } from "../status";
import Notice from "./Notice";
import StatusBadge from "./StatusBadge";

type MailItem = Extract<TimelineItem, { kind: "mail_draft" }>;
type Form = { to: string; subject: string; body: string; attachments: UploadedFile[] };
type Props = {
  taskId: string;
  item: MailItem;
  sendMessage: (message: string, target: MessageTarget) => Promise<ApiError | null>;
  onChanged: () => Promise<void>;
};

const formOf = (item: MailItem): Form => ({
  to: item.draft.to.join("\n"),
  subject: item.draft.subject,
  body: item.draft.body,
  attachments: item.draft.attachments,
});
const fileIds = (files: UploadedFile[]) => files.map((file) => file.file_id);
const sizeLabel = (size: number) => size < 1024 ? `${size} B` : `${(size / 1024).toFixed(1)} KB`;

export default function MailDraftCard({ taskId, item, sendMessage, onChanged }: Props) {
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<Form>(() => formOf(item));
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState<"request" | "save" | "confirm" | "verify" | "upload" | null>(null);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const status = item.execution.status;
  const editable = status === "pending";

  useEffect(() => {
    if (!editing) setForm(formOf(item));
  }, [editing, item.draft.version]);

  const saved = formOf(item);
  const dirty =
    form.to !== saved.to ||
    form.subject !== saved.subject ||
    form.body !== saved.body ||
    fileIds(form.attachments).join("|") !== fileIds(saved.attachments).join("|");
  const recipients = form.to.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);

  const run = async (kind: typeof busy, action: () => Promise<void>) => {
    setBusy(kind);
    setFailure(null);
    try { await action(); }
    catch (error) { setFailure(error instanceof ApiError ? error : new ApiError("offline", String(error), 0)); }
    finally { setBusy(null); }
  };

  const submitRequest = () => run("request", async () => {
    const text = request.trim();
    if (!text) return;
    const error = await sendMessage(text, { kind: "mail_draft", operation_id: item.operation_id });
    if (error !== null) throw error;
    setRequest("");
  });

  const save = () => run("save", async () => {
    await editDraft(item.operation_id, item.draft.version, {
      to: recipients,
      subject: form.subject,
      body: form.body,
      attachment_ids: fileIds(form.attachments),
    });
    setEditing(false);
    await onChanged();
  });

  const confirm = () => run("confirm", async () => {
    await confirmOperation(taskId, item.operation_id, item.draft.version);
    await onChanged();
  });

  const verify = () => run("verify", async () => {
    await verifyExecution(item.operation_id);
    await onChanged();
  });

  const upload = (file: File) => run("upload", async () => {
    const uploaded = await uploadFile(taskId, file);
    setForm((current) => ({ ...current, attachments: [...current.attachments, uploaded] }));
  });

  const discardAndReload = async () => {
    setEditing(false);
    setFailure(null);
    await onChanged();
  };

  const result = item.execution.result;
  return (
    <section className="mail-card" data-component="MailDraftCard">
      <div className="mail-request">
        <input
          value={request}
          placeholder="提出修改要求"
          aria-label="针对这封邮件提出修改要求"
          disabled={!editable || editing || busy !== null}
          onChange={(event) => setRequest(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") { event.preventDefault(); void submitRequest(); }
          }}
        />
        <button type="button" className="icon-action" onClick={() => void submitRequest()}
          disabled={!editable || editing || busy !== null || request.trim() === ""} aria-label="提交修改要求">
          ↑
        </button>
        <StatusBadge badge={operationBadge(status)} />
      </div>

      <div className="mail-fields">
        <label>
          <span>收件人</span>
          {editing ? <textarea value={form.to} aria-label="收件人" onChange={(event) => setForm({ ...form, to: event.target.value })} />
            : <div className="mail-value">{item.draft.to.join("、")}</div>}
        </label>
        <label>
          <span>主题</span>
          {editing ? <input value={form.subject} aria-label="主题" onChange={(event) => setForm({ ...form, subject: event.target.value })} />
            : <div className="mail-value subject">{item.draft.subject}</div>}
        </label>
        <label>
          <span>正文</span>
          {editing ? <textarea className="mail-body-input" value={form.body} aria-label="正文" onChange={(event) => setForm({ ...form, body: event.target.value })} />
            : <div className="mail-body">{item.draft.body}</div>}
        </label>

        <div className="mail-attachments">
          <span className="mail-label">附件</span>
          <div className="attachment-list">
            {(editing ? form.attachments : item.draft.attachments).map((file) => (
              <span className="attachment-chip" key={file.file_id}>
                {file.filename} · {sizeLabel(file.size)}
                {editing && <button type="button" aria-label={`移除 ${file.filename}`} onClick={() => setForm({
                  ...form, attachments: form.attachments.filter((value) => value.file_id !== file.file_id),
                })}>×</button>}
              </span>
            ))}
            {(editing ? form.attachments : item.draft.attachments).length === 0 && <span className="empty-inline">无附件</span>}
            {editing && <label className="btn-secondary file-button">
              {busy === "upload" ? "上传中…" : "添加附件"}
              <input type="file" disabled={busy !== null} onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void upload(file);
                event.target.value = "";
              }} />
            </label>}
          </div>
        </div>
      </div>

      {failure !== null && <div className="mail-notice"><Notice tone="danger" title={
        failure.code === "version_conflict" ? "草稿已被更新，你的修改尚未保存" : "操作失败"
      }>{failure.message}</Notice></div>}

      {item.execution.confirmation !== null && <div className={`mail-result ${status}`}>
        {status === "sending" && "已确认，正在发送。内容已经锁定。"}
        {result?.status === "sent" && `已发送至 ${item.draft.to.join("、")}。`}
        {result?.status === "failed" && `发送失败：${result.reason}。系统不会自动重试。`}
        {result?.status === "unknown" && `结果待核实：${result.reason}。未核实前不能再次发送。`}
        <span>确认于 {shortTime(item.execution.confirmation.confirmed_at)}</span>
      </div>}

      <div className="mail-actions">
        {!editing && <button type="button" className="btn-secondary" disabled={!editable || busy !== null}
          onClick={() => { setForm(formOf(item)); setEditing(true); setFailure(null); }}>编辑</button>}
        {editing && <>
          <button type="button" className="btn" disabled={!dirty || busy !== null} onClick={() => void save()}>
            {busy === "save" ? "保存中…" : "保存"}
          </button>
          <button type="button" className="btn-secondary" disabled={busy !== null}
            onClick={() => void discardAndReload()}>取消</button>
        </>}
        {failure?.code === "version_conflict" && <button type="button" className="btn-secondary" onClick={() => void discardAndReload()}>
          重新载入最新内容
        </button>}
        {!editing && status === "pending" && <button type="button" className="btn" disabled={busy !== null} onClick={() => void confirm()}>
          {busy === "confirm" ? "确认中…" : "确认并发送"}
        </button>}
        {status === "unknown" && <button type="button" className="btn-secondary" disabled={busy !== null} onClick={() => void verify()}>
          {busy === "verify" ? "核实中…" : "核实实际结果"}
        </button>}
      </div>
    </section>
  );
}
