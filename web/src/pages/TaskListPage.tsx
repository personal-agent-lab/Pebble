import SkillPicker from "../features/skills/SkillPicker";
import { emptySelection } from "../features/skills/api";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, createTask, sendMessage } from "../api";
import AppShell, { TaskLinks } from "../components/AppShell";
import Notice from "../components/Notice";
import { useTasks } from "../tasks";

const SEND_ICON = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="12" y1="19" x2="12" y2="5" />
    <polyline points="5 12 12 5 19 12" />
  </svg>
);

/** 输入框跟着内容长高，到上限后改为内部滚动，不把页面顶出视野。 */
const MAX_INPUT_HEIGHT = 200;

/**
 * 新任务入口。任务索引在侧栏，这里只有一件事：写下目标。
 * 自动触发的任务与手动发起的任务都会出现在任务列表，无需手动刷新。
 */
export default function TaskListPage() {
  const navigate = useNavigate();
  const [selection, setSelection] = useState(emptySelection);
  const { error, reload } = useTasks();
  const [goal, setGoal] = useState("");
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<ApiError | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);

  const resize = () => {
    const node = input.current;
    if (node === null) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, MAX_INPUT_HEIGHT)}px`;
  };

  const start = async () => {
    const text = goal.trim();
    if (text === "" || starting) return;
    setStarting(true);
    setStartError(null);
    try {
      const task = await createTask(text);
      // 目标同时作为首条消息交给 Agent；Agent 未接入时任务已建立，提交失败只提示。
      try {
        await sendMessage(task.task_id, text, null, selection);
      } catch (failure) {
        if (!(failure instanceof ApiError) || !failure.unavailable) throw failure;
        setStartError(failure);
      }
      setGoal("");
      void reload();
      navigate(`/tasks/${task.task_id}`);
    } catch (failure) {
      setStartError(failure instanceof ApiError ? failure : new ApiError("offline", String(failure), 0));
    } finally {
      setStarting(false);
    }
  };

  return (
    <AppShell serviceError={error}>
      <div className="hero">
        <div className="hero-inner">
          <h1 className="hero-title">今天要做什么？</h1>

          <div className="starter">
            <textarea
              ref={input}
              className="starter-input"
              rows={1}
              value={goal}
              placeholder="输入目标开始任务…"
              aria-label="新任务目标"
              onChange={(event) => {
                setGoal(event.target.value);
                resize();
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void start();
                }
              }}
            />
            <SkillPicker value={selection} onChange={setSelection} disabled={starting} />
            <div className="starter-foot">
              <span className="starter-tip">Enter 发起 · Shift + Enter 换行</span>
              <button
                type="button"
                className="starter-send"
                onClick={() => void start()}
                disabled={goal.trim() === "" || starting}
                aria-label="发起任务"
              >
                {SEND_ICON}
              </button>
            </div>
          </div>

          {error !== null && (
            <Notice
              tone="danger"
              title="无法连接服务"
              actions={
                <button type="button" className="btn-secondary" onClick={() => void reload()}>
                  重试连接
                </button>
              }
            >
              {error.message}
              <br />
              确认 Pebble 服务已启动后重试。已保存的草稿与待确认内容不会丢失。
            </Notice>
          )}

          {startError !== null && (
            <Notice
              tone={startError.unavailable ? "muted" : "danger"}
              title={startError.unavailable ? "Agent 暂未开放" : "发起失败"}
            >
              {startError.message}
              {startError.unavailable && "。任务已创建，开放后可继续。"}
            </Notice>
          )}

          {/* 手机没有侧栏：任务列表改挂在这里，两端都能从任务页进入已有任务。 */}
          <div className="narrow-only task-nav">
            <div className="task-nav-head">任务列表</div>
            <TaskLinks />
          </div>
        </div>
      </div>
    </AppShell>
  );
}
