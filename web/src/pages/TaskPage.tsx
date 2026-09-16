import { useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";

import { ApiError } from "../api";
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
  const { hash } = useLocation();
  const focusItemId = hash.startsWith("#item-") ? decodeURIComponent(hash.slice("#item-".length)) : null;
  const detail = useTaskDetail(taskId);
  const tasks = useTasks();
  const [message, setMessage] = useState("");
  const [actionError, setActionError] = useState<ApiError | null>(null);

  const onChanged = async () => {
    await detail.reload();
    void tasks.reload();
  };

  const submit = async () => {
    const text = message.trim();
    if (text === "" || detail.sending) return;
    const failure = await detail.send(text, null);
    if (failure === null) {
      setMessage("");
      setActionError(null);
    } else setActionError(failure);
  };

  if (detail.error !== null) return <AppShell serviceError={detail.error}><div className="content">
    <Notice tone="danger" title={detail.error.httpStatus === 404 ? "任务不存在" : "无法连接服务"}
      actions={<button type="button" className="btn-secondary" onClick={() => navigate("/tasks")}>返回任务列表</button>}>
      {detail.error.message}
    </Notice>
  </div></AppShell>;

  const badge = taskBadge(detail.task?.latest_run ?? null, detail.operations);
  const latest = detail.task?.latest_run ?? null;
  const running = latest !== null && (latest.status === "pending" || latest.status === "running");
  // 顶栏标题与侧栏取同一份任务列表：首个调用结束后模型会把目标改写成短标题，
  // 而详情只在事件到达时重读，改写落盘晚于结束事件，靠列表轮询对齐两处文案。
  const listed = tasks.entries?.find((entry) => entry.task.task_id === taskId)?.task ?? null;
  const title = listed?.goal ?? detail.task?.goal ?? null;
  return <AppShell serviceError={detail.error}>
    <div className="chat-top">
      <button type="button" className="back" onClick={() => navigate("/tasks")} aria-label="返回任务列表">{BACK_ICON}</button>
      <div style={{ minWidth: 0, flex: 1 }}>
        <h2 title={title ?? undefined}>{title ?? "读取中…"}</h2>
        <div className="sub">{detail.task !== null && `开始 ${shortTime(detail.task.created_at)}`}</div>
      </div>
      <StatusBadge badge={badge} />
    </div>

    <div className="chat-main"><div className="feed">
      <TimelineFeed key={`${taskId}:${focusItemId ?? ""}`} taskId={taskId} items={detail.items} running={running}
        focusItemId={focusItemId}
        sendMessage={(text, target) => detail.send(text, target)} onChanged={onChanged} />
      {actionError !== null && <Notice tone={actionError.unavailable ? "muted" : "danger"}
        title={actionError.unavailable ? "功能暂未开放" : "操作失败"}>{actionError.message}</Notice>}
    </div></div>

    <div className="msg-composer"><div className="composer-wrap">
      <div className="composer-row">
        <textarea value={message} placeholder="回复 Agent，或补充修改意见…" aria-label="消息" rows={1}
          onChange={(event) => setMessage(event.target.value)} onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); }
          }} />
        <button type="button" className="send-btn" onClick={() => void submit()}
          disabled={message.trim() === "" || detail.sending} aria-label="发送">{SEND_ICON}</button>
      </div>
    </div></div>
  </AppShell>;
}
