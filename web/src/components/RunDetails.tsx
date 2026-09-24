import { useEffect } from "react";
import {
  CaretDown,
  CaretUp,
  CheckCircle,
  CircleNotch,
  Prohibit,
  WarningCircle,
} from "@phosphor-icons/react";

import type { ObservationStep, RunObservation } from "../api";
import { toolCallDisplay } from "./toolCallDisplay";

/** 未记录是明确的显示值：缺失不冒充零，也不推断“没有发生”。 */
const NOT_RECORDED = "未记录";

const DEGRADED_LABELS: Record<string, string> = {
  catalog_failed: "材料目录生成失败",
  manual_skill_not_assembled: "手动选择的技能未装配",
  memory_judge_failed: "记忆判断失败",
  tool_trace_write_failed: "工具轨迹写入失败",
};

function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return NOT_RECORDED;
  if (ms < 1000) return `${Math.round(ms)} 毫秒`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${Math.round(seconds - minutes * 60)} 秒`;
}

function formatNumber(value: number | null | undefined): string {
  return value === null || value === undefined ? NOT_RECORDED : value.toLocaleString("zh-CN");
}

function formatPercent(reading: { used_percentage: number | null } | null): string {
  const value = reading?.used_percentage;
  return value === null || value === undefined ? NOT_RECORDED : `${Math.round(value)}%`;
}

function stepDuration(step: ObservationStep): string {
  if (step.started_at === null) return NOT_RECORDED;
  if (step.ended_at === null) return step.status === "running" ? "" : NOT_RECORDED;
  return formatDuration(new Date(step.ended_at).getTime() - new Date(step.started_at).getTime());
}

function stepLabel(step: ObservationStep): string {
  if (step.kind === "compact") return "上下文压缩";
  if (step.kind === "degraded") {
    return DEGRADED_LABELS[step.code] ?? step.code;
  }
  const display = toolCallDisplay({
    kind: "tool",
    item_id: step.item_id ?? "",
    run_id: "",
    tool_call_id: step.tool_call_id ?? "",
    name: step.code,
    arguments: {},
    status: "ok",
    result: null,
    created_at: "",
  });
  return display.known ? display.label : step.code;
}

function stepStateLabel(step: ObservationStep, runActive: boolean): string {
  if (step.status === "running") return runActive ? "进行中" : "中断时未记录结果";
  if (step.status === "denied") return "已拒绝";
  if (step.status === "error") return "失败";
  return "成功";
}

/** 压缩步骤的前后占用：读数是那一刻的实测值，不保证已经反映压缩结果。 */
function compactReading(step: ObservationStep): string {
  const detail = (step.detail ?? {}) as {
    before?: { used_percentage?: number | null } | null;
    after?: { used_percentage?: number | null } | null;
  };
  const read = (value: { used_percentage?: number | null } | null | undefined): string =>
    value?.used_percentage === null || value?.used_percentage === undefined
      ? NOT_RECORDED
      : `${Math.round(value.used_percentage)}%`;
  return `占用 ${read(detail.before)} → ${read(detail.after)}`;
}

function StepStatusIcon({ status }: { status: ObservationStep["status"] }) {
  if (status === "running") return <CircleNotch size={13} className="run-step-icon pending" aria-hidden />;
  if (status === "denied") return <Prohibit size={13} className="run-step-icon denied" aria-hidden />;
  if (status === "error") return <WarningCircle size={13} weight="fill" className="run-step-icon failed" aria-hidden />;
  return <CheckCircle size={13} weight="fill" className="run-step-icon" aria-hidden />;
}

function Field({ label, value }: { label: string; value: string }) {
  return <div className="run-details-field">
    <dt>{label}</dt>
    <dd>{value}</dd>
  </div>;
}

type Props = {
  run: RunObservation;
  runActive: boolean;
  expanded: boolean;
  focusStepId: string | null;
  onToggle: () => void;
  onLocateItem: (itemId: string) => void;
};

/**
 * 一轮的「执行详情」：装配材料、SDK 时长与用量、上下文占用、压缩与关键降级。
 * 默认折叠；工具步骤与时间线工具行互相定位，缺失值显示「未记录」。
 */
export default function RunDetails({ run, runActive, expanded, focusStepId, onToggle, onLocateItem }: Props) {
  useEffect(() => {
    if (focusStepId !== null && !expanded) onToggle();
  }, [focusStepId, expanded, onToggle]);

  const usage = run.sdk_result?.usage ?? null;
  const summaryParts = [
    `总时长 ${formatDuration(run.sdk_result?.duration_ms ?? null)}`,
    `模型调用 ${usage ? `${usage.length} 次` : NOT_RECORDED}`,
  ];

  return <div className="run-details" id={`run-details-${run.run_id}`}>
    <button type="button" className="run-details-header" onClick={onToggle}
      aria-expanded={expanded} aria-controls={`run-details-body-${run.run_id}`}>
      <span className="run-details-title">执行详情</span>
      <span className="run-details-summary">{summaryParts.join(" · ")}</span>
      {expanded
        ? <CaretUp size={12} weight="bold" aria-hidden />
        : <CaretDown size={12} weight="bold" aria-hidden />}
    </button>
    {expanded && <div className="run-details-body" id={`run-details-body-${run.run_id}`}>
      <section className="run-details-section">
        <h4>材料</h4>
        {run.materials === null ? <p className="run-details-empty">{NOT_RECORDED}</p> : <>
          {run.materials.assembled.length === 0 && <p className="run-details-empty">本轮没有装配材料</p>}
          {run.materials.assembled.length > 0 && <ul className="run-details-materials">
            {run.materials.assembled.map((material) => <li key={material.title}>
              <span className="run-details-material-title">{material.title}</span>
              <span className="run-details-material-chars">{material.chars.toLocaleString("zh-CN")} 字</span>
            </li>)}
          </ul>}
          {run.materials.skipped.length > 0 && <ul className="run-details-skipped">
            {run.materials.skipped.map((skipped, index) => <li key={index}>
              跳过{skipped.category}{skipped.skill_id ? `（${skipped.skill_id}）` : ""}：{skipped.reason}
            </li>)}
          </ul>}
        </>}
      </section>

      <section className="run-details-section">
        <h4>时长与调用</h4>
        <dl className="run-details-fields">
          <Field label="总时长" value={formatDuration(run.sdk_result?.duration_ms ?? null)} />
          <Field label="API 时长" value={formatDuration(run.sdk_result?.duration_api_ms ?? null)} />
          <Field label="模型调用次数" value={usage ? `${usage.length} 次` : NOT_RECORDED} />
          <Field label="结束原因" value={run.sdk_result?.stop_reason ?? NOT_RECORDED} />
        </dl>
      </section>

      <section className="run-details-section">
        <h4>用量</h4>
        {usage === null || usage.length === 0 ? <p className="run-details-empty">{NOT_RECORDED}</p> : <>
          <table className="run-details-usage">
            <thead><tr><th>请求</th><th>输入</th><th>输出</th><th>Credits</th></tr></thead>
            <tbody>
              {usage.map((entry, index) => <tr key={entry.request_id ?? entry.message_id ?? index}>
                <td title={entry.request_id ?? entry.message_id ?? undefined}>#{index + 1}</td>
                <td>{formatNumber(entry.input_tokens)}</td>
                <td>{formatNumber(entry.output_tokens)}</td>
                <td>{formatNumber(entry.credits)}</td>
              </tr>)}
            </tbody>
          </table>
          <p className="run-details-totals">
            合计 {formatNumber(run.usage_totals.input_tokens)} 输入 ·&nbsp;
            {formatNumber(run.usage_totals.output_tokens)} 输出 ·&nbsp;
            {formatNumber(run.usage_totals.credits)} Credits
            {(run.usage_totals.input_tokens === null
              || run.usage_totals.output_tokens === null
              || run.usage_totals.credits === null) && <span className="run-details-note">（有请求缺字段或无法排除重复）</span>}
          </p>
        </>}
      </section>

      <section className="run-details-section">
        <h4>上下文占用</h4>
        <dl className="run-details-fields">
          <Field label="轮开始" value={formatPercent(run.context_before)} />
          <Field label="轮结束" value={formatPercent(run.context_after)} />
        </dl>
      </section>

      {run.steps.length > 0 && <section className="run-details-section">
        <h4>步骤</h4>
        <ul className="run-details-steps">
          {run.steps.map((step) => <li key={step.step_id}
            className={`run-step${focusStepId === step.step_id ? " focused" : ""}`}
            data-step-id={step.step_id}>
            <StepStatusIcon status={step.status} />
            <span className="run-step-name">{stepLabel(step)}</span>
            {step.kind === "compact" && <span className="run-step-detail">{compactReading(step)}</span>}
            <span className={`run-step-state${step.status === "denied" ? " denied" : ""}${step.status === "error" ? " failed" : ""}`}>
              {stepStateLabel(step, runActive)}
            </span>
            <span className="run-step-duration">{stepDuration(step)}</span>
            {step.kind === "tool" && step.item_id !== null
              && <button type="button" className="run-step-locate"
                onClick={() => onLocateItem(step.item_id as string)}>定位</button>}
          </li>)}
        </ul>
      </section>}
    </div>}
  </div>;
}
