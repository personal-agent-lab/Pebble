import { useEffect, useState } from "react";

import { ApiError, confirmOperation, editCalendarPreview, verifyExecution, type MessageTarget, type TimelineItem } from "../api";
import { operationBadge } from "../status";
import StatusBadge from "./StatusBadge";

type Item = Extract<TimelineItem, { kind: "calendar_preview" }>;
type Fields = Omit<Item["preview"], "operation_id" | "version" | "status">;

export default function CalendarPreviewCard({ taskId, item, sendMessage, onChanged }: {
  taskId: string; item: Item;
  sendMessage: (message: string, target: MessageTarget) => Promise<ApiError | null>;
  onChanged: () => Promise<void>;
}) {
  const saved: Fields = { calendar_id: "primary", summary: item.preview.summary, start: item.preview.start,
    end: item.preview.end, all_day: item.preview.all_day, location: item.preview.location,
    description: item.preview.description };
  const [form, setForm] = useState<Fields>(saved);
  const [version, setVersion] = useState(item.preview.version);
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => { if (version !== item.preview.version) { setForm(saved); setVersion(item.preview.version); } }, [item.preview.version]);
  const dirty = JSON.stringify(form) !== JSON.stringify(saved);
  const editable = item.execution.status === "pending";
  const act = async (kind: "save" | "confirm" | "verify") => {
    setBusy(true); setError(null);
    try {
      if (kind === "save") await editCalendarPreview(item.operation_id, item.preview.version, form);
      else if (kind === "confirm") await confirmOperation(taskId, item.operation_id, item.preview.version);
      else await verifyExecution(item.operation_id);
    } catch (failure) { setError(failure instanceof ApiError ? failure : new ApiError("unexpected", String(failure), 0)); }
    finally { await onChanged(); setBusy(false); }
  };
  const ask = async () => {
    if (!request.trim()) return;
    setBusy(true); const failure = await sendMessage(request.trim(), { kind: "calendar_preview", operation_id: item.operation_id });
    setError(failure); if (!failure) setRequest(""); setBusy(false);
  };
  return <section className="op-card" data-component="CalendarPreviewCard">
    <div className="op-head"><span className="t">日程预览</span><StatusBadge badge={operationBadge(item.execution.status)} /></div>
    <div className="confirm-body"><div className="fields">
      <label className="field"><span className="k">标题</span><input aria-label="标题" className="input" value={form.summary} disabled={!editable || busy} onChange={e => setForm({...form, summary:e.target.value})}/></label>
      <label className="field"><span className="k">全天</span><input aria-label="全天" type="checkbox" checked={form.all_day} disabled={!editable || busy} onChange={e => setForm({...form, all_day:e.target.checked})}/></label>
      <label className="field"><span className="k">开始</span><input aria-label="开始" className="input" value={form.start} disabled={!editable || busy} onChange={e => setForm({...form, start:e.target.value})}/></label>
      <label className="field"><span className="k">结束</span><input aria-label="结束" className="input" value={form.end} disabled={!editable || busy} onChange={e => setForm({...form, end:e.target.value})}/></label>
      <label className="field"><span className="k">地点</span><input aria-label="地点" className="input" value={form.location ?? ""} disabled={!editable || busy} onChange={e => setForm({...form, location:e.target.value || null})}/></label>
      <label className="field"><span className="k">备注</span><textarea aria-label="备注" className="input body" value={form.description} disabled={!editable || busy} onChange={e => setForm({...form, description:e.target.value})}/></label>
    </div><aside className="side"><h5>实际影响</h5><p>确认后只在你的 iCloud 主日历中创建这一项日程，不邀请或通知其他人。</p>
      {item.execution.result && "event_id" in item.execution.result && <p>事件编号：{String(item.execution.result.event_id)}</p>}
      {item.execution.result && "reason" in item.execution.result && <p>{String(item.execution.result.reason)}</p>}
    </aside></div>
    {error && <p role="alert" className="field-error">{error.message}</p>}
    {editable && <div className="op-foot"><button className="btn-secondary" disabled={busy || !dirty} onClick={() => void act("save")}>保存修改</button><button className="btn" disabled={busy || dirty} onClick={() => void act("confirm")}>确认创建</button></div>}
    {item.execution.status === "unknown" && <div className="op-foot"><button className="btn-secondary" disabled={busy} onClick={() => void act("verify")}>核实结果</button></div>}
    {editable && <div className="op-foot"><input className="input" value={request} placeholder="告诉 Agent 怎样修改这项日程" onChange={e => setRequest(e.target.value)}/><button className="btn-secondary" disabled={busy || !request.trim()} onClick={() => void ask()}>请 Agent 修改</button></div>}
  </section>;
}
