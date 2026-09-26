import { CaretDown, CaretRight } from "@phosphor-icons/react";

import type { RunObservation } from "../api";

const NOT_RECORDED = "未记录";

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return NOT_RECORDED;
  if (ms < 1000) return `${Math.round(ms)} 毫秒`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${Math.round(seconds - minutes * 60)} 秒`;
}

type Props = {
  /** 这一轮的运行观测，只用来取时长；没有观测时显示「未记录」。 */
  run?: RunObservation;
  expanded: boolean;
  onToggle: () => void;
};

/**
 * 已结束一轮的折叠头：一行「已思考 X」加通栏分隔线，把过程区与结果隔开。
 *
 * 这里只报这一轮的时长。材料、用量与步骤清单不再单独成卡片；上下文占用移到
 * 输入框左侧的标记里（contracts/observability.md §4）。
 */
export default function RunDuration({ run, expanded, onToggle }: Props) {
  const duration = run?.sdk_result?.duration_ms ?? null;
  return <div className="run-details-head">
    <button type="button" className="run-details-header" onClick={onToggle}
      aria-expanded={expanded}>
      <span className="run-details-title">已思考 {formatDuration(duration)}</span>
      {expanded
        ? <CaretDown size={12} weight="bold" aria-hidden />
        : <CaretRight size={12} weight="bold" aria-hidden />}
    </button>
    <div className="run-details-divider" aria-hidden />
  </div>;
}
