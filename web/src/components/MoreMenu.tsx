import { useEffect, useRef, useState, type ReactNode } from "react";

/** ⋯ 菜单里的一项：动作由调用方给，菜单只管展开、定位与键盘。 */
export type MoreMenuItem = {
  key: string;
  label: string;
  icon?: ReactNode;
  /** 破坏性动作（删除、归档）用红色，与普通动作区分。 */
  danger?: boolean;
  onSelect: () => void;
};

const MORE_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="currentColor">
    <circle cx="5" cy="12" r="1.7" />
    <circle cx="12" cy="12" r="1.7" />
    <circle cx="19" cy="12" r="1.7" />
  </svg>
);

/**
 * 行尾与页头的“⋯”菜单：资料的列表行、资料页顶栏与技能页顶栏共用。
 *
 * 弹层用 fixed 定位，坐标在打开时按按钮量一次，不被列表或页面的滚动区裁掉。
 */
export function MoreMenu({ items, disabledReason, label = "更多操作" }: {
  items: MoreMenuItem[];
  /** 给出时整份菜单禁用，并在末尾说明原因（例如“先保存或放弃修改”）。 */
  disabledReason?: string;
  label?: string;
}) {
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const open = anchor !== null;

  useEffect(() => {
    if (!open) return;
    const close = () => setAnchor(null);
    const onPointerDown = (event: MouseEvent) => {
      if (root.current !== null && !root.current.contains(event.target as Node)) close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const toggle = () => {
    if (open) {
      setAnchor(null);
      return;
    }
    const rect = button.current?.getBoundingClientRect();
    if (rect === undefined) return;
    setAnchor({ top: rect.bottom + 4, right: document.documentElement.clientWidth - rect.right });
  };

  const pick = (action: () => void) => {
    setAnchor(null);
    action();
  };

  const disabled = disabledReason !== undefined;
  return (
    <div className={`kb-more${open ? " open" : ""}`} ref={root}>
      <button type="button" ref={button} className="kb-more-button" aria-label={label}
        aria-haspopup="menu" aria-expanded={open} onClick={toggle}>
        {MORE_ICON}
      </button>
      {anchor !== null && (
        <div className="task-menu kb-more-menu" role="menu" title={disabledReason}
          style={{ position: "fixed", top: anchor.top, right: anchor.right }}>
          {items.map((item) => (
            <button key={item.key} type="button" role="menuitem"
              className={`task-menu-item${item.danger ? " danger" : ""}`} disabled={disabled}
              onClick={() => pick(item.onSelect)}>
              {item.icon}{item.label}
            </button>
          ))}
          {disabled && <div className="kb-more-hint">{disabledReason}</div>}
        </div>
      )}
    </div>
  );
}
