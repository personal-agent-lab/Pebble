import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";

import { ApiError, deleteTask } from "../api";
import { useSeen } from "../seen";
import { taskBadge } from "../status";
import { useTasks } from "../tasks";

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

const SEARCH_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="11" cy="11" r="7" />
    <line x1="21" y1="21" x2="16.65" y2="16.65" />
  </svg>
);

const COMPOSE_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" />
  </svg>
);

/** 邮件触发的会话在列表里带这个标记，用户自己发起的不带。 */
const MAIL_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="5" width="18" height="14" rx="2" />
    <polyline points="3.5 7 12 13 20.5 7" />
  </svg>
);

const MORE_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="currentColor">
    <circle cx="5" cy="12" r="1.7" />
    <circle cx="12" cy="12" r="1.7" />
    <circle cx="19" cy="12" r="1.7" />
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
    <circle cx="12" cy="8" r="4" />
    <path d="M4 21a8 8 0 0 1 16 0" />
  </svg>
);

const SKILL_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="4 17 10 11 4 5" />
    <line x1="12" y1="19" x2="20" y2="19" />
  </svg>
);

// Skill 属于后续阶段，服务端尚无接口：入口保留但明确置灰，不做占位页面。
const PLACEHOLDERS = [
  { label: "Skill", icon: SKILL_ICON },
];

/** 收起时显示的任务条数：够认出最近在做什么，又不会把下面的入口顶出视野。 */
const COLLAPSED_COUNT = 5;

const EXPANDED_KEY = "pebble.nav.tasks.expanded";

/** 展开状态跨页面、跨刷新保留；本地存储不可用时只是记不住，不影响使用。 */
function readExpanded(): boolean {
  try {
    return window.localStorage.getItem(EXPANDED_KEY) === "1";
  } catch {
    return false;
  }
}

function writeExpanded(expanded: boolean): void {
  try {
    window.localStorage.setItem(EXPANDED_KEY, expanded ? "1" : "0");
  } catch {
    /* 无痕模式等场景下写入被拒绝，忽略即可 */
  }
}

/** 任务分区包括任务列表、单个任务与从列表进入的搜索。 */
function inTasksSection(pathname: string): boolean {
  return pathname === "/tasks" || pathname.startsWith("/tasks/") || pathname === "/search";
}

/** 任务分区里最后停留的位置；切到资料、记忆再点回任务时回到这里，而不是任务列表。 */
let lastTasksPath = "/tasks";

/**
 * 手机底部 tab 用的任务入口：没有子列表，整个任务分区都算在内。
 * 从别的分区点回来时恢复上次停留的页面；已在任务分区内时点它回到任务列表。
 */
function TasksLink({ unread }: { unread: number }) {
  const { pathname, search } = useLocation();
  const active = inTasksSection(pathname);
  if (active) lastTasksPath = pathname + search;
  return (
    <NavLink to={active ? "/tasks" : lastTasksPath} className={`nav-item${active ? " active" : ""}`}>
      {TASKS_ICON}
      任务
      {unread > 0 && <span className="nav-count">{unread} 待确认</span>}
    </NavLink>
  );
}

/** 资料入口：资料列表与每份资料的页面都算在内。 */
function KbLink() {
  return (
    <NavLink to="/kb" className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
      {KB_ICON}
      资料
    </NavLink>
  );
}

/** 长期记忆入口：Agent 每轮都会带上的“关于你”与“事实与约定”。 */
function MemoryLink() {
  return (
    <NavLink to="/memory" className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
      {MEMORY_ICON}
      记忆
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
 * 条目右侧的三点菜单：悬浮出现，收起时只放“删除对话”，确认后整条任务连同记录一起删除。
 * 删除失败（如任务仍在运行）把原因留在菜单里，不弹全局提示。
 */
function TaskItemMenu({ taskId, onDeleted }: { taskId: string; onDeleted: () => void }) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);

  const close = () => {
    setOpen(false);
    setConfirming(false);
    setFailure(null);
  };

  /** 侧栏任务区自己会滚动，弹层留在流里会被裁掉：改用 fixed，坐标在打开时按按钮量一次。 */
  const openMenu = () => {
    const rect = button.current?.getBoundingClientRect();
    if (rect === undefined) return;
    // 用 clientWidth 而不是 innerWidth：后者含滚动条宽度，fixed 的右边界不含，经典滚动条下会偏。
    setAnchor({ top: rect.bottom + 4, right: document.documentElement.clientWidth - rect.right });
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (root.current !== null && !root.current.contains(event.target as Node)) close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    // 量出来的坐标不跟随滚动与窗口变化：菜单是一次性操作，位置会失效就直接收起。
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const remove = async () => {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    try {
      await deleteTask(taskId);
      close();
      onDeleted();
    } catch (error) {
      setFailure(error instanceof ApiError ? error.message : String(error));
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nav-task-menu" ref={root}>
      <button
        type="button"
        ref={button}
        className="nav-task-more"
        aria-label="更多操作"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => (open ? close() : openMenu())}
      >
        {MORE_ICON}
      </button>
      {open && anchor !== null && (
        <div
          className="task-menu"
          role="menu"
          style={{ position: "fixed", top: anchor.top, right: anchor.right }}
        >
          {failure !== null && <div className="task-menu-error">{failure}</div>}
          {confirming ? (
            <>
              <button
                type="button"
                role="menuitem"
                className="task-menu-item danger"
                disabled={busy}
                onClick={() => void remove()}
              >
                确认删除
              </button>
              <button
                type="button"
                role="menuitem"
                className="task-menu-item"
                disabled={busy}
                onClick={() => setConfirming(false)}
              >
                取消
              </button>
            </>
          ) : (
            <button
              type="button"
              role="menuitem"
              className="task-menu-item danger"
              onClick={() => setConfirming(true)}
            >
              删除对话
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * 任务列表本身就是导航，列表项直接进入对应任务。
 *
 * 这是索引不是总览，状态与时间留在任务页；行内只标出没读过的待确认，因为它需要用户动作，
 * 点进去读过就不再提醒（真实状态仍在任务页顶部）。
 * PC 放在侧栏，列出全部任务、由列表自己滚动；手机没有侧栏，同一组件出现在任务页里，
 * 那里默认只列最近几条，其余折在“展开显示”后面，免得把输入框推出一屏。
 */
export function TaskLinks({ limited = true }: { limited?: boolean }) {
  const { entries, reload } = useTasks();
  const seen = useSeen();
  const [expanded, setExpanded] = useState(readExpanded);
  const { pathname } = useLocation();
  const navigate = useNavigate();

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    writeExpanded(next);
  };

  const onDeleted = async (taskId: string) => {
    await reload();
    if (pathname.startsWith(`/tasks/${taskId}`)) navigate("/tasks");
  };

  const all = entries ?? [];
  const visible = !limited || expanded ? [...all] : all.slice(0, COLLAPSED_COUNT);
  // 正在查看的任务始终留在列表里，收起时也不会从列表消失。
  const current = all.find((entry) => pathname.startsWith(`/tasks/${entry.task.task_id}`));
  if (current !== undefined && !visible.includes(current)) visible.push(current);
  // 列表里出现邮件标记时，没有标记的行也留出同一条槽位，任务名左边才对得齐；
  // 一条邮件任务都没有时不留，免得每行都带一段没有来由的缩进。
  const reserveSource = visible.some((entry) => entry.task.source === "mail");

  // 列表在自己的区域里滚动：从搜索或别处打开一条靠后的任务时，把它滚进视野。
  const list = useRef<HTMLDivElement>(null);
  const currentId = current?.task.task_id;
  useEffect(() => {
    if (currentId === undefined) return;
    list.current?.querySelector(".nav-task.active")?.scrollIntoView({ block: "nearest" });
  }, [currentId]);

  return (
    <>
      <div className="nav-list" ref={list}>
        {entries === null && <div className="nav-note">读取中…</div>}
        {entries !== null && all.length === 0 && <div className="nav-note">还没有任务</div>}
        {visible.map((entry) => {
          const badge = taskBadge(entry.latestRun, entry.operations);
          const unread = seen.unread(entry.task.task_id, entry.operations);
          // 待确认优先：它需要用户动作，新结果只是提醒去看。
          const fresh = unread === 0 && seen.fresh(entry.task.task_id, entry.latestRun);
          const fromMail = entry.task.source === "mail";
          return (
            <div className="nav-task-row" key={entry.task.task_id}>
              <NavLink
                to={`/tasks/${entry.task.task_id}`}
                title={`${entry.task.goal} · ${badge.label}${fromMail ? " · 由新邮件触发" : ""}`}
                className={({ isActive }) => `nav-task${isActive ? " active" : ""}`}
              >
                {reserveSource && (
                  <span className="nav-task-source">{fromMail && MAIL_ICON}</span>
                )}
                <span className="t">{entry.task.goal}</span>
                {fromMail && <span className="sr-only">由新邮件触发</span>}
                {unread > 0 && <span className="nav-dot wait" aria-hidden />}
                {fresh && <span className="nav-dot fresh" aria-hidden />}
                {fresh && <span className="sr-only">有新结果</span>}
                <span className="sr-only">{badge.label}</span>
              </NavLink>
              <TaskItemMenu
                taskId={entry.task.task_id}
                onDeleted={() => void onDeleted(entry.task.task_id)}
              />
            </div>
          );
        })}
      </div>

      {limited && all.length > COLLAPSED_COUNT && (
        <button type="button" className="nav-more" aria-expanded={expanded} onClick={toggle}>
          {expanded ? "收起显示" : "展开显示"}
        </button>
      )}
    </>
  );
}

/**
 * 侧栏任务区：排在资料、记忆等固定入口之后，占满侧栏剩余高度，任务多了在列表内滚动，
 * 固定入口始终留在原位。“任务”是分区小标题而不是页面入口，没有选中态；
 * 待确认计数和搜索、发起新任务的入口挂在标题上，列表滚到哪里都看得到。
 */
function SidebarTasks() {
  const { entries } = useTasks();
  const unread = useSeen().unreadTotal(entries);

  return (
    <div className="nav-group">
      <div className="nav-head">
        <span className="nav-head-title">任务</span>
        {unread > 0 && <span className="nav-head-count">{unread} 待确认</span>}
        <NavLink
          to="/search"
          title="搜索对话"
          aria-label="搜索对话"
          className={({ isActive }) => `nav-new nav-search${isActive ? " active" : ""}`}
        >
          {SEARCH_ICON}
        </NavLink>
        <NavLink
          to="/tasks"
          end
          title="发起新任务"
          aria-label="发起新任务"
          className={({ isActive }) => `nav-new${isActive ? " active" : ""}`}
        >
          {COMPOSE_ICON}
        </NavLink>
      </div>
      <TaskLinks limited={false} />
    </div>
  );
}

/** PC 侧栏 / 手机底部 tab 共用同一组导航项，两端功能一致。 */
export default function AppShell({ serviceError, children }: Props) {
  const { entries, error } = useTasks();
  const unread = useSeen().unreadTotal(entries);
  const offline = serviceError?.offline === true || error?.offline === true;

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">P</div>
          <div className="brand-name">Pebble</div>
        </div>
        <nav className="sidebar-nav">
          <KbLink />
          <MemoryLink />
          <Placeholders />
          <SidebarTasks />
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
        <TasksLink unread={unread} />
        <KbLink />
        <MemoryLink />
        <Placeholders />
      </nav>
    </div>
  );
}
