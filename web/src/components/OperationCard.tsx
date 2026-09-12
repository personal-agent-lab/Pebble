import { Link } from "react-router-dom";

import { effectiveStatus, type OperationView } from "../hooks";
import { operationBadge } from "../status";
import StatusBadge from "./StatusBadge";

const MAIL_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" />
    <polyline points="22,6 12,13 2,6" />
  </svg>
);

type Props = {
  taskId: string;
  view: OperationView;
  confirming: boolean;
  onConfirm: () => void;
};

/**
 * 对话内的待确认卡片。内容来自已保存草稿，刷新或重新进入后原样恢复；
 * 副作用与绑定版本写在卡片内，确认按钮始终标出所确认的版本。
 */
export default function OperationCard({ taskId, view, confirming, onConfirm }: Props) {
  const status = effectiveStatus(view);
  const draft = view.draft;
  const version = view.execution?.version ?? view.summary.version;

  return (
    <div className="op-card" data-component="PendingOpCard">
      <div className="op-head">
        {MAIL_ICON}
        <span className="t">回复邮件草稿</span>
        <span className="v">草稿 v{version}</span>
        <StatusBadge badge={operationBadge(status)} />
      </div>
      <div className="op-body">
        {draft === null ? (
          <div>草稿内容读取失败，刷新后重试。</div>
        ) : (
          <div className="fld">
            <span className="k">to</span>
            <span className="val">{draft.to.join("、")}</span>
            <span className="k">subject</span>
            <span className="val">{draft.subject}</span>
            <span className="k">body</span>
            <span className="val clip">{draft.body}</span>
          </div>
        )}
        副作用：确认后将以你的 Gmail 账号向上述收件人发送，内容与所确认的 v{version} 逐字段一致。
      </div>
      <div className="op-actions">
        <button type="button" className="btn" onClick={onConfirm} disabled={status !== "pending" || confirming}>
          {confirming ? "确认中…" : `确认发送（v${version}）`}
        </button>
        <Link className="btn-secondary" to={`/tasks/${taskId}/confirm`}>
          编辑草稿
        </Link>
      </div>
    </div>
  );
}
