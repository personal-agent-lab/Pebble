import { useEffect, useState } from "react";
import { ApiError, confirmOperation, editCalendarDraft, type CalendarFields } from "../api";
import { effectiveStatus, type OperationView } from "../hooks";
import { operationBadge } from "../status";
import StatusBadge from "./StatusBadge";

const fields: [keyof CalendarFields, string][] = [
  ["title", "标题"], ["start", "开始时间"], ["end", "结束时间"],
  ["timezone", "时区"], ["location", "地点"], ["description", "备注"],
];
const empty: CalendarFields = { title: "", start: "", end: "", timezone: "", location: "", description: "" };
function values(view: OperationView): CalendarFields {
  return Object.fromEntries(fields.map(([key]) => [key, view.calendarDraft?.[key] ?? ""])) as CalendarFields;
}
export default function CalendarEditor({ taskId, view, onChanged }: {
  taskId: string; view: OperationView; onChanged: () => Promise<void>;
}) {
  const draft = view.calendarDraft;
  const [form, setForm] = useState<CalendarFields>(() => draft ? values(view) : empty);
  const [version, setVersion] = useState(draft?.version);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    if (draft?.version !== version) { setForm(values(view)); setVersion(draft?.version); }
  }, [draft, version, view]);
  const status = effectiveStatus(view);
  const editable = status === "pending" && draft !== null;
  const dirty = JSON.stringify(form) !== JSON.stringify(values(view));
  const act = async (confirm: boolean) => {
    if (version === undefined) return;
    setBusy(true); setError(null);
    try {
      if (confirm) await confirmOperation(taskId, view.summary.operation_id, version);
      else await editCalendarDraft(view.summary.operation_id, version, form);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause : new ApiError("offline", String(cause), 0));
    } finally { await onChanged(); setBusy(false); }
  };
  const result = view.execution?.result;
  return <section className="op-card" data-component="CalendarDraftCard">
    <div className="op-head"><span className="t">日程预览</span><StatusBadge badge={operationBadge(status)} /></div>
    <div className="confirm-body">
      <div className="fields">
        {!draft && <p>预览读取失败，请刷新重试。</p>}
        {fields.map(([key, label]) => <label className="field" key={key}>
          <span className="k">{label}</span>
          {key === "description" ? <textarea className="input body" value={form[key]} disabled={!editable || busy} onChange={e => setForm({...form, [key]: e.target.value})} /> :
            <input className="input" value={form[key]} disabled={!editable || busy} onChange={e => setForm({...form, [key]: e.target.value})} />}
          {error?.fieldErrors?.filter(e => e.field === key).map(e => <span className="field-error" key={e.field}>{e.message}</span>)}
        </label>)}
      </div>
      <aside className="side"><h5>目标日历</h5><p style={{overflowWrap: "anywhere"}}>{draft?.calendar_url}</p>
        <h5>时间格式</h5><p>起止时间包含日期和 UTC 偏移；时区使用 IANA 名称，例如 Australia/Adelaide。</p>
        <h5>副作用说明</h5><p>确认后创建上述日程。未检查日历冲突；不会发送邀请或邮件。</p>
        {view.execution?.confirmation && <p>确认时间：{view.execution.confirmation.confirmed_at}</p>}
        {result?.status === "created" && <p style={{overflowWrap: "anywhere"}}>已创建，UID：{result.uid}</p>}
        {result && "reason" in result && <p>{result.reason}。不会自动再次创建。</p>}
      </aside>
    </div>
    {error && <p role="alert" style={{padding: "0 18px"}}>{error.code === "version_conflict" ? "预览已更新，请审阅最新内容后重新操作。" : error.message}</p>}
    <div className="op-foot">
      <button className="btn" disabled={!editable || busy || dirty} onClick={() => void act(true)}>确认创建</button>
      <button className="btn-secondary" disabled={!editable || busy || !dirty} onClick={() => void act(false)}>保存修改</button>
      <span className="note">{dirty ? "请先保存修改，再审阅确认。" : "确认仅适用于当前已保存内容。"}</span>
    </div>
  </section>;
}
