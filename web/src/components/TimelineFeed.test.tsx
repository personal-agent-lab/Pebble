// @vitest-environment jsdom

import { render, screen } from "@testing-library/react";
import { beforeAll, expect, test, vi } from "vitest";

import type { TimelineItem } from "../api";
import TimelineFeed from "./TimelineFeed";

beforeAll(() => { Element.prototype.scrollIntoView = vi.fn(); });

const draft: Extract<TimelineItem, { kind: "mail_draft" }> = {
  item_id: "card-1",
  kind: "mail_draft",
  run_id: "run-1",
  operation_id: "op-1",
  created_at: "2026-09-14T00:00:00Z",
  draft: {
    operation_id: "op-1", kind: "new", version: 1, status: "pending",
    to: ["office@example.edu"], subject: "证明文件", body: "请查收完整证明文件。",
  },
  execution: { operation_id: "op-1", version: 1, status: "pending", confirmation: null, result: null },
};

test("按持久化顺序渲染 Agent 文字、完整邮件卡和后续文字", () => {
  const items: TimelineItem[] = [
    { item_id: "text-1", kind: "text", role: "assistant", run_id: "run-1", text: "前置说明", created_at: "2026-09-14T00:00:00Z" },
    draft,
    { item_id: "text-2", kind: "text", role: "assistant", run_id: "run-1", text: "后置说明", created_at: "2026-09-14T00:00:01Z" },
  ];
  const { container } = render(<TimelineFeed taskId="task-1" items={items} sendMessage={vi.fn()} onChanged={vi.fn()} />);
  const text = container.textContent ?? "";
  expect(text.indexOf("前置说明")).toBeLessThan(text.indexOf("证明文件"));
  expect(text.indexOf("证明文件")).toBeLessThan(text.indexOf("后置说明"));
  expect(screen.getByText("请查收完整证明文件。")).toBeTruthy();
});
