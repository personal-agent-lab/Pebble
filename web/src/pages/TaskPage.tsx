import { useEffect, useMemo, useState, type ReactNode } from "react";
import SkillUsage from "../features/skills/SkillUsage";
import SkillPicker from "../features/skills/SkillPicker";
import { emptySelection } from "../features/skills/api";
import { useLocation, useNavigate, useParams } from "react-router-dom";

import { cachedModels, listModels, type ModelEntry, type TimelineItem } from "../api";
import AppShell from "../components/AppShell";
import Composer from "../components/Composer";
import Notice from "../components/Notice";
import StatusBadge from "../components/StatusBadge";
import TimelineFeed from "../components/TimelineFeed";
import { useTaskDetail } from "../hooks";
import {
  forgetTask, retryable, retryTask, reviseLostTask, usePendingTask, viewTask, type PendingTask,
} from "../pendingTasks";
import { shortTime, taskBadge } from "../status";
import { useTasks } from "../tasks";

const BACK_ICON = <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><polyline points="15 18 9 12 15 6" /></svg>;
const NO_OP = async () => null;

export default function TaskPage() {
  const { taskId = "" } = useParams();
  const pending = usePendingTask(taskId);
  const models = useModels();
  // 创建请求还没成功：只用本地记录展示，不向服务端读这个尚不存在的任务。
  if (pending !== null && pending.status !== "created") {
    return <PendingTaskView key={taskId} pending={pending} models={models} />;
  }
  return <TaskDetailView key={taskId} taskId={taskId} placeholder={pending} models={models} />;
}

/** 目录只用来把固定型号显示成名称；读取失败时退回显示型号标识。 */
function useModels() {
  const [models, setModels] = useState<ModelEntry[] | null>(cachedModels);
  useEffect(() => {
    void listModels().then((catalog) => setModels(catalog.models))
      .catch(() => setModels((current) => current ?? []));
  }, []);
  return models;
}

/** 新建任务在服务端读到之前的用户气泡，与时间线里的用户消息同一种样式。 */
function usePlaceholderItems(pending: PendingTask | null): TimelineItem[] {
  const [createdAt] = useState(() => new Date().toISOString());
  const urls = useMemo(() => pending?.files.map((file) => URL.createObjectURL(file)) ?? [], [pending?.files]);
  useEffect(() => () => { for (const url of urls) URL.revokeObjectURL(url); }, [urls]);
  if (pending === null) return [];
  return [{
    item_id: `pending-${pending.taskId}`,
    kind: "text",
    role: "user",
    run_id: "",
    text: pending.message,
    created_at: createdAt,
    attachments: pending.files.map((file, index) => ({
      file_id: `pending-${index}`,
      filename: file.name,
      mime_type: file.type,
      size: file.size,
      sha256: "",
      url: urls[index],
    })),
  }];
}

function TaskHeader({ title, sub, children }: { title: string | null; sub: string; children: ReactNode }) {
  const navigate = useNavigate();
  return <div className="chat-top">
    <button type="button" className="back" onClick={() => navigate("/tasks")} aria-label="返回任务列表">{BACK_ICON}</button>
    <div style={{ minWidth: 0, flex: 1 }}>
      <h2 title={title ?? undefined}>{title ?? "读取中…"}</h2>
      <div className="sub">{sub}</div>
    </div>
    {children}
  </div>;
}

/**
 * 正在创建或创建失败的任务。失败按原因给出操作：连接或服务问题可原样重试
 * （同一任务标识，服务端去重）；输入被拒绝只能改了再发，退回首页输入框。
 */
function PendingTaskView({ pending, models }: { pending: PendingTask; models: ModelEntry[] | null }) {
  const navigate = useNavigate();
  const items = usePlaceholderItems(pending);
  useEffect(() => viewTask(pending.taskId), [pending.taskId]);
  const error = pending.status === "failed" ? pending.error : null;
  const title = pending.message || pending.files[0]?.name || null;
  return <AppShell serviceError={null}>
    <TaskHeader title={title} sub={error === null ? "正在发送…" : "未发送"}>
      <StatusBadge badge={error === null ? { tone: "run", label: "处理中" } : { tone: "err", label: "未发送" }} />
    </TaskHeader>

    <div className="chat-main"><div className="feed">
      <TimelineFeed taskId={pending.taskId} items={items} running={error === null}
        sendMessage={NO_OP} onChanged={async () => undefined} />
      {error !== null && <div className="send-failure" role="alert">
        <span className="error-text">发送失败：{error.message}</span>
        <div className="send-failure-actions">
          {retryable(error) && <button type="button" className="btn-secondary"
            onClick={() => retryTask(pending.taskId)}>重试</button>}
          {/* 离开页面时失败的消息会退回首页输入框，这里只需跳转。 */}
          <button type="button" className="btn-secondary" onClick={() => navigate("/tasks")}>编辑后重发</button>
        </div>
      </div>}
    </div></div>

    <div className="msg-composer"><div className="composer-wrap">
      <Composer placeholder="随心输入" sending model={pending.model} models={models ?? []}
        modelsPending={models === null} modelLocked onSubmit={NO_OP} />
    </div></div>
  </AppShell>;
}

function TaskDetailView({ taskId, placeholder, models }: {
  taskId: string; placeholder: PendingTask | null; models: ModelEntry[] | null;
}) {
  const navigate = useNavigate();
  const { hash } = useLocation();
  const focusItemId = hash.startsWith("#item-") ? decodeURIComponent(hash.slice("#item-".length)) : null;
  const detail = useTaskDetail(taskId);
  const [selection, setSelection] = useState(emptySelection);
  const tasks = useTasks();
  const placeholderItems = usePlaceholderItems(detail.task === null ? placeholder : null);

  // 刚创建成功：让侧栏立即出现这个任务，不等下一轮列表轮询。
  const justCreated = placeholder !== null;
  const reloadTasks = tasks.reload;
  useEffect(() => { if (justCreated) void reloadTasks(); }, [justCreated, reloadTasks]);
  const loaded = detail.task !== null;
  useEffect(() => { if (loaded) forgetTask(taskId); }, [loaded, taskId]);

  // 刷新时创建请求还没到服务端：任务读不到，就把文字退回首页输入框重新发送。
  const missing = detail.error?.httpStatus === 404;
  useEffect(() => {
    if (missing && reviseLostTask(taskId)) navigate("/tasks", { replace: true });
  }, [missing, taskId, navigate]);

  const onChanged = async () => {
    await detail.reload();
    void tasks.reload();
  };

  if (detail.error !== null) return <AppShell serviceError={detail.error}><div className="content">
    <Notice tone="danger" title={detail.error.httpStatus === 404 ? "任务不存在" : "无法连接服务"}
      actions={<button type="button" className="btn-secondary" onClick={() => navigate("/tasks")}>返回任务列表</button>}>
      {detail.error.message}
    </Notice>
  </div></AppShell>;

  const badge = taskBadge(detail.task?.latest_run ?? null, detail.operations);
  const latest = detail.task?.latest_run ?? null;
  const waiting = !loaded && placeholder !== null;
  const running = waiting || (latest !== null && (latest.status === "pending" || latest.status === "running"));
  // 顶栏标题与侧栏取同一份任务列表：首个调用结束后模型会把目标改写成短标题，
  // 而详情只在事件到达时重读，改写落盘晚于结束事件，靠列表轮询对齐两处文案。
  const listed = tasks.entries?.find((entry) => entry.task.task_id === taskId)?.task ?? null;
  const title = listed?.goal ?? detail.task?.goal
    ?? (placeholder !== null ? placeholder.message || placeholder.files[0]?.name || null : null);
  return <AppShell serviceError={detail.error}>
    <TaskHeader title={title} sub={detail.task !== null ? `开始 ${shortTime(detail.task.created_at)}` : ""}>
      <StatusBadge badge={waiting ? { tone: "run", label: "处理中" } : badge} />
    </TaskHeader>

    <div className="chat-main"><div className="feed">
      <SkillUsage taskId={taskId} refresh={detail.items} />
      <TimelineFeed key={`${taskId}:${focusItemId ?? ""}`} taskId={taskId}
        items={waiting ? placeholderItems : detail.items} running={running}
        activity={running ? detail.activity : null}
        focusItemId={focusItemId}
        retryRunId={latest?.retryable === true ? latest.run_id : null}
        retrying={detail.retrying} retryMessage={detail.retry}
        sendMessage={(text, target) => detail.send(text, target)} onChanged={onChanged} />
    </div></div>

    <div className="msg-composer"><div className="composer-wrap">
      <SkillPicker value={selection} onChange={setSelection} disabled={detail.sending} />
      <Composer placeholder="随心输入" sending={detail.sending} model={detail.task?.model ?? placeholder?.model ?? ""}
        models={models ?? []} modelsPending={models === null} modelLocked onSubmit={async (text, files) => {
          // 对话框里的消息让待确认的草稿失效：侧栏圆点跟着立即更新，不等下一次轮询。
          const error = await detail.send(text, null, files, selection);
          if (error === null) { setSelection(emptySelection()); void tasks.reload(); }
          return error;
        }} />
    </div></div>
  </AppShell>;
}
