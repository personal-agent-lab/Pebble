import { useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  ApiError,
  confirmOperation,
  editDraft,
  type MessageTarget,
  type TimelineItem,
  verifyExecution,
} from "../api";
import { operationBadge, shortTime } from "../status";
import Notice from "./Notice";
import StatusBadge from "./StatusBadge";

type MailItem = Extract<TimelineItem, { kind: "mail_draft" }>;
type Form = { to: string; subject: string; body: string };
type Props = {
  taskId: string;
  item: MailItem;
  sendMessage: (message: string, target: MessageTarget) => Promise<ApiError | null>;
  onChanged: () => Promise<void>;
};

const ASK_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" />
  </svg>
);

const SEND_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M22 2 11 13" />
    <path d="M22 2 15 22l-4-9-9-4z" />
  </svg>
);

/** 收件人在卡片上是一行文本：分隔符收得宽松，回显统一成规范写法。 */
const splitRecipients = (value: string) => value.split(/[\s,;，；、]+/).filter(Boolean);
const formOf = (item: MailItem): Form => ({
  to: item.draft.to.join(", "),
  subject: item.draft.subject,
  body: item.draft.body,
});
const changed = (a: Form, b: Form) => a.to !== b.to || a.subject !== b.subject || a.body !== b.body;
const asApiError = (error: unknown) =>
  error instanceof ApiError ? error : new ApiError("offline", String(error), 0);

export default function MailDraftCard({ taskId, item, sendMessage, onChanged }: Props) {
  const [form, setForm] = useState<Form>(() => formOf(item));
  const [asking, setAsking] = useState(false);
  const [request, setRequest] = useState("");
  const [toOpen, setToOpen] = useState(false);
  const [busy, setBusy] = useState<"request" | "confirm" | "verify" | null>(null);
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);

  const status = item.execution.status;
  const editable = status === "pending";

  // 草稿没有独立的“编辑态”：字段常驻可编辑，失焦即按当前版本写回。
  const formRef = useRef(form);
  const savedRef = useRef<Form>(formOf(item));
  const versionRef = useRef(item.draft.version);
  const flightRef = useRef<Promise<number> | null>(null);
  const subjectRef = useRef<HTMLTextAreaElement>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  formRef.current = form;

  const dirty = changed(form, savedRef.current);
  const recipients = splitRecipients(form.to);

  // 服务端产生新版本时回填。本地还有未保存内容时既不覆盖内容也不推进版本号，
  // 下一次保存仍会撞上版本冲突，由用户决定是重新载入还是继续改。
  useEffect(() => {
    if (changed(formRef.current, savedRef.current)) return;
    savedRef.current = formOf(item);
    versionRef.current = item.draft.version;
    setForm(formOf(item));
  }, [item.draft.version]);

  // 主题和正文都随内容撑高：长主题要能折行，卡片里也不出现内层滚动条。
  // 用 layout effect 量：先置 auto 再读 scrollHeight 会让输入框瞬间塌回一行，
  // 放在 paint 之后做，打字时就能看见这一帧的抖动。
  useLayoutEffect(() => {
    for (const node of [subjectRef.current, bodyRef.current]) {
      if (node === null) continue;
      node.style.height = "auto";
      node.style.height = `${node.scrollHeight}px`;
    }
  }, [form.subject, form.body]);

  // 返回值区分“真的写了一版”和“本来就没改动”：前者才有资格清掉上一条失败提示。
  const flush = async (): Promise<{ version: number; saved: boolean }> => {
    if (flightRef.current !== null) await flightRef.current.catch(() => undefined);
    const current = formRef.current;
    if (!changed(current, savedRef.current)) return { version: versionRef.current, saved: false };
    const task = editDraft(item.operation_id, versionRef.current, {
      to: splitRecipients(current.to),
      subject: current.subject,
      body: current.body,
    }).then((result) => {
      savedRef.current = current;
      versionRef.current = result.version;
      return result.version;
    });
    flightRef.current = task;
    setSaving(true);
    try {
      const version = await task;
      await onChanged();
      return { version, saved: true };
    } finally {
      flightRef.current = null;
      setSaving(false);
    }
  };

  const run = (kind: Exclude<typeof busy, null>, action: () => Promise<void>) => {
    setBusy(kind);
    setFailure(null);
    void (async () => {
      try { await action(); }
      catch (error) { setFailure(asApiError(error)); }
      finally { setBusy(null); }
    })();
  };

  // 失焦即存。失败提示只在保存成功后才清除：在这里提前清掉会让底部的
  // “重新载入”按钮在 mousedown 引发的失焦里先卸载，用户那一下点了个空。
  const autosave = () => {
    if (!editable || !dirty) return;
    void flush().then(
      (outcome) => { if (outcome.saved) setFailure(null); },
      (error) => setFailure(asApiError(error)),
    );
  };

  const submitRequest = () => run("request", async () => {
    const text = request.trim();
    if (text === "") return;
    const error = await sendMessage(text, { kind: "mail_draft", operation_id: item.operation_id });
    if (error !== null) throw error;
    setRequest("");
    setAsking(false);
  });

  const confirm = () => run("confirm", async () => {
    const { version } = await flush();
    await confirmOperation(taskId, item.operation_id, version);
    await onChanged();
  });

  const verify = () => run("verify", async () => {
    await verifyExecution(item.operation_id);
    await onChanged();
  });

  const reload = async () => {
    savedRef.current = formOf(item);
    versionRef.current = item.draft.version;
    setForm(formOf(item));
    setFailure(null);
    await onChanged();
  };

  const result = item.execution.result;
  return (
    <section className="mail-card" data-component="MailDraftCard">
      <div className="mail-toolbar">
        {asking ? (
          <div className="mail-ask">
            <input
              value={request}
              autoFocus
              placeholder="提出修改要求"
              aria-label="针对这封邮件提出修改要求"
              disabled={busy !== null}
              onChange={(event) => setRequest(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") { event.preventDefault(); submitRequest(); }
                if (event.key === "Escape") { setRequest(""); setAsking(false); }
              }}
            />
            <button type="button" className="icon-action" aria-label="提交修改要求"
              disabled={busy !== null || request.trim() === ""} onClick={submitRequest}>↑</button>
          </div>
        ) : (
          <button type="button" className="mail-ghost" disabled={!editable || busy !== null}
            onClick={() => setAsking(true)}>{ASK_ICON}<span>修改要求</span></button>
        )}

        <StatusBadge badge={operationBadge(status)} />

        {editable && <button type="button" className="mail-send"
          disabled={busy !== null || recipients.length === 0} onClick={confirm}>
          {SEND_ICON}<span>{busy === "confirm" ? "确认中…" : "确认并发送"}</span>
        </button>}
        {status === "unknown" && <button type="button" className="btn-secondary mail-verify"
          disabled={busy !== null} onClick={verify}>
          {busy === "verify" ? "核实中…" : "核实实际结果"}
        </button>}
      </div>

      <div className="mail-head">
        {toOpen || !editable ? (
          <div className="mail-row">
            <span className="mail-label">收件人</span>
            <input className="mail-to-input" value={form.to} aria-label="收件人" autoFocus={toOpen}
              disabled={!editable} placeholder="邮箱地址，多个用逗号分隔"
              onChange={(event) => setForm({ ...form, to: event.target.value })} onBlur={autosave} />
          </div>
        ) : (
          <button type="button" className="mail-row mail-row-open" onClick={() => setToOpen(true)}>
            <span className="mail-label">收件人</span>
            <span className={`mail-value${recipients.length === 0 ? " placeholder" : ""}`}>
              {recipients.length === 0 ? "添加收件人" : recipients.join("、")}
            </span>
          </button>
        )}
      </div>

      <div className="mail-compose">
        <textarea ref={subjectRef} className="mail-subject-input" value={form.subject} aria-label="主题"
          rows={1} disabled={!editable} placeholder="主题"
          onChange={(event) => setForm({ ...form, subject: event.target.value })}
          onKeyDown={(event) => { if (event.key === "Enter") event.preventDefault(); }}
          onBlur={autosave} />
        <textarea ref={bodyRef} className="mail-body-input" value={form.body} aria-label="正文"
          disabled={!editable} placeholder="正文"
          onChange={(event) => setForm({ ...form, body: event.target.value })} onBlur={autosave} />
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

      <div className="mail-foot">
        {failure?.code === "version_conflict" && <button type="button" className="btn-secondary"
          onClick={() => void reload()}>重新载入最新内容</button>}
        {saving && <span className="mail-saving">保存中…</span>}
      </div>
    </section>
  );
}
