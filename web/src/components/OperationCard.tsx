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
};

/**
 * 对话内的待确认卡片。内容来自已保存草稿，刷新或重新进入后原样恢复；
 * 卡片只显示摘要，完整审阅、编辑与确认统一在确认页完成。
 */
export default function OperationCard({ taskId, view }: Props) {
  const status = effectiveStatus(view);
  const draft = view.draft;

  return (
    <div className="op-card" data-component="PendingOpCard">
      <div className="op-head">
        {MAIL_ICON}
        <span className="t">回复邮件草稿</span>
        <StatusBadge badge={operationBadge(status)} />
      </div>
      <div className="op-body">
        {draft === null ? (
          <div>草稿内容读取失败，刷新后重试。</div>
        ) : (
          <div className="fld">
            <span className="k">收件人</span>
            <span className="val">{draft.to.join("、")}</span>
            <span className="k">主题</span>
            <span className="val">{draft.subject}</span>
            <span className="k">正文</span>
            <span className="val clip">{draft.body}</span>
          </div>
        )}
        请查看完整草稿后确认发送。
      </div>
      <div className="op-actions">
        <Link className="btn" to={`/tasks/${taskId}/confirm`}>
          审阅与确认
        </Link>
      </div>
    </div>
  );
}
