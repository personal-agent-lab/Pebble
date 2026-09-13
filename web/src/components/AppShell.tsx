import { useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";

import type { ApiError } from "../api";
import { taskBadge } from "../status";
import { pendingTotal, useTasks } from "../tasks";

type Props = {
  serviceError?: ApiError | null;
  children: ReactNode;
};

const TASKS_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="22 12 16 12 14 15 10 15 8 12 2 12" />
    <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
  </svg>
);

const CHEVRON_ICON = (
  <svg className="i chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="9 18 15 12 9 6" />
  </svg>
);

const KB_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
    <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
  </svg>
);

const MEMORY_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6" />
  </svg>
);

const SKILL_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="4 17 10 11 4 5" />
    <line x1="12" y1="19" x2="20" y2="19" />
  </svg>
);

// 资料、规则与 Skill 属于后续阶段，服务端尚无接口：入口保留但明确置灰，不做占位页面。
const PLACEHOLDERS = [
  { label: "资料", icon: KB_ICON },
  { label: "规则", icon: MEMORY_ICON },
  { label: "Skill", icon: SKILL_ICON },
];

const OPEN_KEY = "pebble.nav.tasks";

/** 展开状态跨页面、跨刷新保留；本地存储不可用时只是记不住，不影响使用。 */
function readOpen(): boolean {
  try {
    return window.localStorage.getItem(OPEN_KEY) !== "0";
  } catch {
    return true;
  }
}

function writeOpen(open: boolean): void {
  try {
    window.localStorage.setItem(OPEN_KEY, open ? "1" : "0");
  } catch {
    /* 无痕模式等场景下写入被拒绝，忽略即可 */
  }
}

/** 侧栏内任务列表自己标出所处任务，入口只在总览页高亮；底部 tab 没有子项，整个分区都算在内。 */
function TasksLink({ pending, exact }: { pending: number; exact?: boolean }) {
  return (
    <NavLink to="/tasks" end={exact} className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
      {TASKS_ICON}
      任务
      {pending > 0 && <span className="nav-count">{pending} 待确认</span>}
    </NavLink>
  );
}

function Placeholders() {
  return (
    <>
      {PLACEHOLDERS.map((item) => (
        <button key={item.label} type="button" className="nav-item disabled" disabled title="暂未开放">
          {item.icon}
          {item.label}
          <span className="nav-count">暂未开放</span>
        </button>
      ))}
    </>
  );
}

/**
 * 侧栏任务区：入口可展开为任务列表，收起后只剩一行入口。
 * 列表项直接进入对应任务，待确认的任务带强调点，无需先回到总览页。
 */
function SidebarTasks() {
  const { entries } = useTasks();
  const [open, setOpen] = useState(readOpen);
  const pending = pendingTotal(entries);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    writeOpen(next);
  };

  return (
    <div className="nav-group">
      <div className="nav-row">
        <TasksLink pending={pending} exact />
        <button
          type="button"
          className={`nav-disc${open ? " open" : ""}`}
          aria-expanded={open}
          aria-controls="nav-task-list"
          aria-label={open ? "收起任务列表" : "展开任务列表"}
          onClick={toggle}
        >
          {CHEVRON_ICON}
        </button>
      </div>

      {open && (
        <div className="nav-list" id="nav-task-list">
          {entries === null && <div className="nav-note">读取中…</div>}
          {entries !== null && entries.length === 0 && <div className="nav-note">还没有任务</div>}
          {entries?.map((entry) => {
            const badge = taskBadge(entry.latestRun, entry.operations);
            return (
              <NavLink
                key={entry.task.task_id}
                to={`/tasks/${entry.task.task_id}`}
                title={`${entry.task.goal} · ${badge.label}`}
                className={({ isActive }) => `nav-task${isActive ? " active" : ""}`}
              >
                <span className={`nav-dot ${badge.tone}`} aria-hidden />
                <span className="t">{entry.task.goal}</span>
                <span className="sr-only">{badge.label}</span>
              </NavLink>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** PC 侧栏 / 手机底部 tab 共用同一组导航项，两端功能一致。 */
export default function AppShell({ serviceError, children }: Props) {
  const { entries, error } = useTasks();
  const offline = serviceError?.offline === true || error?.offline === true;

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">P</div>
          <div>
            <div className="brand-name">Pebble</div>
            <div className="brand-sub">personal agent</div>
          </div>
        </div>
        <nav className="sidebar-nav">
          <SidebarTasks />
          <Placeholders />
        </nav>
        {offline && (
          <div className="sidebar-foot">
            <div className="health">
              <span className="off">服务未连接</span>
            </div>
          </div>
        )}
      </aside>

      <div className="main">{children}</div>

      <nav className="tabbar">
        <TasksLink pending={pendingTotal(entries)} />
        <Placeholders />
      </nav>
    </div>
  );
}
