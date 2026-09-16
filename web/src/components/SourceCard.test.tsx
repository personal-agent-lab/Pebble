// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, expect, test, vi } from "vitest";

import type { TimelineItem } from "../api";
import TimelineFeed from "./TimelineFeed";

beforeAll(() => { Element.prototype.scrollIntoView = vi.fn(); });
afterEach(cleanup);

const END = { taskId: "task-1", running: false, sendMessage: () => Promise.resolve(null), onChanged: async () => {} };

const source = (id: string, path: string, title: string, heading: string | null) => ({
  source_id: `s-${id}`,
  title,
  excerpt: `${title}的原文片段`,
  ref: {
    id,
    path,
    heading,
    lines: [18, 26] as [number, number],
    commit: "9f2c1ab7d3e4f5061728394a5b6c7d8e9f0a1b2c",
  },
});

test("回答下的来源卡展示标题、路径、分节、行号、版本与原文片段", () => {
  const items: TimelineItem[] = [
    {
      item_id: "text-1", kind: "text", role: "assistant", run_id: "run-1",
      text: "依据资料，验收代号是 CORAL-7421。", created_at: "2026-09-14T00:00:00Z",
      sources: [source("kb_1", "kb/项目/验收.md", "验收纪要", "验收纪要 / 验收结果")],
    },
  ];
  render(<TimelineFeed {...END} items={items} />);

  expect(screen.getByText("资料来源")).toBeTruthy();
  expect(screen.getByText("验收纪要")).toBeTruthy();
  expect(screen.getByText("kb/项目/验收.md")).toBeTruthy();
  expect(screen.getByText("验收纪要 / 验收结果")).toBeTruthy();
  expect(screen.getByText("L18–26")).toBeTruthy();
  expect(screen.getByText("9f2c1ab")).toBeTruthy();
  expect(screen.getByText("验收纪要的原文片段")).toBeTruthy();
});

test("多条来源按读取顺序排列，长路径与长原文不撑破消息栏", () => {
  const longPath = `kb/项目/${"很长的目录名/".repeat(6)}验收.md`;
  const items: TimelineItem[] = [
    {
      item_id: "text-1", kind: "text", role: "assistant", run_id: "run-1",
      text: "两份资料的说法不一致。", created_at: "2026-09-14T00:00:00Z",
      sources: [
        source("kb_1", longPath, "第一份", null),
        { ...source("kb_2", "kb/项目/第二份.md", "第二份", "第二份 / 结论"),
          excerpt: "很长的原文片段。".repeat(80) },
      ],
    },
  ];
  const { container } = render(<TimelineFeed {...END} items={items} />);

  const paths = screen.getAllByText(longPath);
  expect(paths.length).toBe(1);
  const excerpts = container.querySelectorAll(".source-excerpt");
  expect(excerpts.length).toBe(2);
  // 分节为空的来源只展示已有信息，不出现空占位
  const metas = container.querySelectorAll(".source-item")[0].querySelectorAll(".source-meta > span");
  expect([...metas].map((node) => node.textContent)).toEqual([longPath, "L18–26", "9f2c1ab"]);
});

test("没有来源的回答不渲染来源卡", () => {
  const items: TimelineItem[] = [
    { item_id: "text-1", kind: "text", role: "assistant", run_id: "run-1", text: "今天天气不错。", created_at: "2026-09-14T00:00:00Z" },
    { item_id: "text-2", kind: "text", role: "assistant", run_id: "run-1", text: "要出门吗？", created_at: "2026-09-14T00:00:01Z", sources: [] },
  ];
  const { container } = render(<TimelineFeed {...END} items={items} />);

  expect(container.querySelector(".source-card")).toBeNull();
});

test("来源只跟随它挂上的那一段回答", () => {
  const items: TimelineItem[] = [
    { item_id: "text-1", kind: "text", role: "user", run_id: "run-1", text: "帮我查资料", created_at: "2026-09-14T00:00:00Z" },
    { item_id: "text-2", kind: "text", role: "assistant", run_id: "run-1", text: "先查一下。", created_at: "2026-09-14T00:00:01Z" },
    {
      item_id: "text-3", kind: "text", role: "assistant", run_id: "run-1",
      text: "查到了。", created_at: "2026-09-14T00:00:02Z",
      sources: [source("kb_1", "kb/项目/验收.md", "验收纪要", null)],
    },
  ];
  const { container } = render(<TimelineFeed {...END} items={items} />);

  const cards = container.querySelectorAll(".source-card");
  expect(cards.length).toBe(1);
  expect(container.querySelectorAll(".msg")[1].querySelector(".source-card")).toBeNull();
  expect(container.querySelectorAll(".msg")[2].querySelector(".source-card")).toBeTruthy();
});
