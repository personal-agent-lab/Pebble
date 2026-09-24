// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, expect, test, vi } from "vitest";
import { useState } from "react";

import type { RunObservation } from "../api";
import RunDetails from "./RunDetails";

const scrollIntoView = vi.fn();
beforeAll(() => { Element.prototype.scrollIntoView = scrollIntoView; });
afterEach(cleanup);

/** 面板的展开状态由外层控制；测试里用同一形状驱动。 */
function Harness({ run, runActive = false }: { run: RunObservation; runActive?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  return <RunDetails run={run} runActive={runActive} expanded={expanded} focusStepId={null}
    onToggle={() => setExpanded((value) => !value)} onLocateItem={vi.fn()} />;
}

const run = (overrides: Partial<RunObservation> = {}): RunObservation => ({
  run_id: "run-1",
  kind: "message",
  status: "done",
  model: "auto",
  created_at: "2026-09-24T10:00:00Z",
  started_at: "2026-09-24T10:00:01Z",
  finished_at: "2026-09-24T10:00:09Z",
  materials: {
    assembled: [
      { title: "关于你", chars: 120 },
      { title: "本轮材料", chars: 40 },
    ],
    skipped: [{ category: "资料目录", reason: "生成失败" }],
  },
  sdk_result: {
    duration_ms: 8200,
    duration_api_ms: 5100,
    num_turns: 3,
    is_error: false,
    usage: [
      {
        message_id: "m1", request_id: "r1", input_tokens: 1100, output_tokens: 220, credits: 0.5,
      },
    ],
  },
  usage_totals: { input_tokens: 1100, output_tokens: 220, credits: 0.5 },
  context_before: { used_percentage: 42, threshold_percentage: 80, auto_compact_enabled: false },
  context_after: { used_percentage: 58, threshold_percentage: 80, auto_compact_enabled: false },
  steps: [
    {
      step_id: "s1", kind: "tool", code: "gmail_search", status: "ok",
      started_at: "2026-09-24T10:00:02Z", ended_at: "2026-09-24T10:00:04Z",
      item_id: "item-1", tool_call_id: "c1", detail: { source: "mcp", chars: 900 },
    },
    {
      step_id: "s2", kind: "tool", code: "WebSearch", status: "denied",
      started_at: null, ended_at: "2026-09-24T10:00:05Z",
      item_id: "item-2", tool_call_id: "c2", detail: { source: "builtin" },
    },
    {
      step_id: "s3", kind: "tool", code: "WebFetch", status: "running",
      started_at: "2026-09-24T10:00:06Z", ended_at: null,
      item_id: "item-3", tool_call_id: "c3", detail: { source: "builtin" },
    },
    {
      step_id: "s4", kind: "compact", code: "compact", status: "ok",
      started_at: "2026-09-24T10:00:07Z", ended_at: "2026-09-24T10:00:08Z",
      item_id: null, tool_call_id: null,
      detail: { auto: true, before: { used_percentage: 85 }, after: { used_percentage: 25 } },
    },
    {
      step_id: "s5", kind: "degraded", code: "memory_judge_failed", status: "ok",
      started_at: "2026-09-24T10:00:09Z", ended_at: "2026-09-24T10:00:09Z",
      item_id: null, tool_call_id: null, detail: null,
    },
  ],
  ...overrides,
});

test("默认折叠；展开后显示材料、用量、上下文与缺失值「未记录」", async () => {
  const emptyRun = run({
    materials: null,
    sdk_result: null,
    usage_totals: { input_tokens: null, output_tokens: null, credits: null },
    context_before: null,
    context_after: null,
    steps: [],
  });
  render(<Harness run={emptyRun} />);

  expect(screen.queryByText("材料")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: /执行详情/ }));
  expect(screen.getByText("材料")).toBeTruthy();
  // 摘要与空值都明确显示「未记录」，不冒充零。
  expect(screen.getByText(/总时长 未记录 · 模型调用 未记录/)).toBeTruthy();
});

test("展开后展示装配与跳过的材料、用量合计和上下文占用", () => {
  render(<RunDetails run={run()} runActive={false} expanded={true} focusStepId={null}
    onToggle={vi.fn()} onLocateItem={vi.fn()} />);

  expect(screen.getByText("关于你")).toBeTruthy();
  expect(screen.getByText("120 字")).toBeTruthy();
  expect(screen.getByText(/跳过资料目录：生成失败/)).toBeTruthy();
  expect(screen.getByText("8.2 秒")).toBeTruthy();
  expect(screen.getByText("5.1 秒")).toBeTruthy();
  expect(screen.getByText("1 次")).toBeTruthy();
  expect(screen.getByText("1,100")).toBeTruthy();
  expect(screen.getByText("58%")).toBeTruthy();
});

test("步骤区分成功、已拒绝、进行中与压缩、降级；工具步骤可定位", async () => {
  const locate = vi.fn();
  render(<RunDetails run={run()} runActive={false} expanded={true} focusStepId={null}
    onToggle={vi.fn()} onLocateItem={locate} />);

  expect(screen.getByText("已拒绝")).toBeTruthy();
  // 已结束轮里留下的 running 步骤显示中断语义。
  expect(screen.getByText("中断时未记录结果")).toBeTruthy();
  expect(screen.getByText("上下文压缩")).toBeTruthy();
  // 压缩行直接给出前后占用读数，不靠点开才知道压缩了什么。
  expect(screen.getByText("占用 85% → 25%")).toBeTruthy();
  expect(screen.getByText("记忆判断失败")).toBeTruthy();

  const buttons = screen.getAllByRole("button", { name: "定位" });
  await userEvent.click(buttons[0]);
  expect(locate).toHaveBeenCalledWith("item-1");
});

test("压缩步骤缺读数时显示「未记录」，不冒充零", () => {
  render(<RunDetails run={run({ steps: [{
    step_id: "s1", kind: "compact", code: "compact", status: "ok",
    started_at: "2026-09-24T10:00:07Z", ended_at: "2026-09-24T10:00:08Z",
    item_id: null, tool_call_id: null, detail: { auto: true, before: null, after: null },
  }] })} runActive={false} expanded={true} focusStepId={null}
    onToggle={vi.fn()} onLocateItem={vi.fn()} />);
  expect(screen.getByText("占用 未记录 → 未记录")).toBeTruthy();
});

test("运行中的轮里 running 步骤显示进行中", () => {
  render(<RunDetails run={run({ status: "running" })} runActive={true} expanded={true}
    focusStepId={null} onToggle={vi.fn()} onLocateItem={vi.fn()} />);
  expect(screen.getByText("进行中")).toBeTruthy();
  expect(screen.queryByText("中断时未记录结果")).toBeNull();
});
