import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { ApiError, type UploadedFile, uploadFile } from "../api";
import AppShell from "../components/AppShell";
import Notice from "../components/Notice";
import StatusBadge from "../components/StatusBadge";
import TimelineFeed from "../components/TimelineFeed";
import { useTaskDetail } from "../hooks";
import { shortTime, taskBadge } from "../status";
import { useTasks } from "../tasks";

const BACK_ICON = <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><polyline points="15 18 9 12 15 6" /></svg>;
const SEND_ICON = <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><line x1="22" y1="2" x2="11" y2="13" /><polygon points="22 2 15 22 11 13 2 9 22 2" /></svg>;

export default function TaskPage() {
  const { taskId = "" } = useParams();
  const navigate = useNavigate();
  const detail = useTaskDetail(taskId);
  const tasks = useTasks();
  const [message, setMessage] = useState("");
  const [attachments, setAttachments] = useState<UploadedFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [actionError, setActionError] = useState<ApiError | null>(null);

  const onChanged = async () => {
    await detail.reload();
    void tasks.reload();
  };

  const submit = async () => {
    const text = message.trim();
    if (text === "" || detail.sending || uploading) return;
    const failure = await detail.send(text, null, attachments.map((file) => file.file_id));
    if (failure === null) {
      setMessage("");
      setAttachments([]);
      setActionError(null);
    } else setActionError(failure);
  };

  const addAttachment = async (file: File) => {
    setUploading(true);
    setActionError(null);
    try {
      const uploaded = await uploadFile(taskId, file);
      setAttachments((current) => [...current, uploaded]);
    }
    catch (error) { setActionError(error instanceof ApiError ? error : new ApiError("offline", String(error), 0)); }
    finally { setUploading(false); }
  };

  if (detail.error !== null) return <AppShell serviceError={detail.error}><div className="content">
    <Notice tone="danger" title={detail.error.httpStatus === 404 ? "任务不存在" : "无法连接服务"}
      actions={<button type="button" className="btn-secondary" onClick={() => navigate("/tasks")}>返回任务列表</button>}>
      {detail.error.message}
    </Notice>
  </div></AppShell>;

  const badge = taskBadge(detail.task?.latest_run ?? null, detail.operations);
  return <AppShell serviceError={detail.error}>
    <div className="chat-top">
      <button type="button" className="back" onClick={() => navigate("/tasks")} aria-label="返回任务列表">{BACK_ICON}</button>
      <div style={{ minWidth: 0, flex: 1 }}>
        <h2>{detail.task?.goal ?? "读取中…"}</h2>
        <div className="sub">{detail.task !== null && `开始 ${shortTime(detail.task.created_at)}`}</div>
      </div>
      <StatusBadge badge={badge} />
    </div>

    <div className="chat-main"><div className="feed">
      <TimelineFeed taskId={taskId} items={detail.items}
        sendMessage={(text, target) => detail.send(text, target)} onChanged={onChanged} />
      {actionError !== null && <Notice tone={actionError.unavailable ? "muted" : "danger"}
        title={actionError.unavailable ? "功能暂未开放" : "操作失败"}>{actionError.message}</Notice>}
    </div></div>

    <div className="msg-composer"><div className="composer-wrap">
      {attachments.length > 0 && <div className="composer-attachments">{attachments.map((file) =>
        <span className="attachment-chip" key={file.file_id}>{file.filename}
          <button type="button" aria-label={`移除 ${file.filename}`} onClick={() => setAttachments((current) => current.filter((value) => value.file_id !== file.file_id))}>×</button>
        </span>)}</div>}
      <div className="composer-row">
        <label className="attach-button" aria-label="添加附件">＋<input type="file" disabled={uploading || detail.sending}
          onChange={(event) => { const file = event.target.files?.[0]; if (file) void addAttachment(file); event.target.value = ""; }} /></label>
        <textarea value={message} placeholder="回复 Agent，或补充修改意见…" aria-label="消息" rows={1}
          onChange={(event) => setMessage(event.target.value)} onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); }
          }} />
        <button type="button" className="send-btn" onClick={() => void submit()}
          disabled={message.trim() === "" || detail.sending || uploading} aria-label="发送">{SEND_ICON}</button>
      </div>
    </div></div>
  </AppShell>;
}
