import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

import type { ApiError } from "../api";

type Props = {
  pendingCount?: number;
  serviceError?: ApiError | null;
  children: ReactNode;
};

const TASKS_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="22 12 16 12 14 15 10 15 8 12 2 12" />
    <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
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

function Nav({ pendingCount }: { pendingCount?: number }) {
  return (
    <>
      <NavLink to="/tasks" className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
        {TASKS_ICON}
        任务
        {pendingCount !== undefined && pendingCount > 0 && (
          <span className="nav-count">{pendingCount} 待确认</span>
        )}
      </NavLink>
      {PLACEHOLDERS.map((item) => (
        <button
          key={item.label}
          type="button"
          className="nav-item disabled"
          disabled
          title="尚未接入，本阶段不提供"
        >
          {item.icon}
          {item.label}
          <span className="nav-count">未接入</span>
        </button>
      ))}
    </>
  );
}

/** PC 侧栏 / 手机底部 tab 共用同一组导航项，两端功能一致。 */
export default function AppShell({ pendingCount, serviceError, children }: Props) {
  const offline = serviceError?.offline === true;

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
        <nav>
          <Nav pendingCount={pendingCount} />
        </nav>
        <div className="sidebar-foot">
          <div className="health">
            {offline ? (
              <span className="off">服务未连接</span>
            ) : (
              <>
                <span className="k">服务运行中</span>
                <br />
                任务不依赖页面保持打开
              </>
            )}
          </div>
        </div>
      </aside>

      <div className="main">{children}</div>

      <nav className="tabbar">
        <Nav pendingCount={pendingCount} />
      </nav>
    </div>
  );
}
