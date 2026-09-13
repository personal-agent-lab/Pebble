import type { ReactNode } from "react";

type Props = {
  tone?: "default" | "danger" | "muted";
  title?: string;
  children: ReactNode;
  actions?: ReactNode;
};

/** 提示块：连接失败、依赖未接入、版本冲突与校验失败共用。 */
export default function Notice({ tone = "default", title, children, actions }: Props) {
  return (
    <div className={`notice${tone === "default" ? "" : ` ${tone}`}`} role="status">
      <div>
        {title !== undefined && <span className="t">{title}</span>}
        {children}
        {actions !== undefined && <div className="actions">{actions}</div>}
      </div>
    </div>
  );
}
