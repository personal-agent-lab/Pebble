// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test } from "vitest";

import type { SkillUsageRecord } from "../api";
import SkillUsage from "./SkillUsage";

const usage: SkillUsageRecord[] = [
  { run_id: "run-1", skill_id: "weekly-report", revision: "r1", source: "auto", loaded_at: "2026-09-22T01:00:00Z" },
  { run_id: "run-1", skill_id: "meeting-notes", revision: "r2", source: "manual", loaded_at: "2026-09-22T01:00:01Z" },
];

afterEach(cleanup);

test("无加载记录时不显示面板", () => {
  const { container } = render(<SkillUsage usage={[]} />);
  expect(container.querySelector(".skill-usage")).toBeNull();
});

test("展示加载的技能与来源，展开后按轮去重", async () => {
  render(<SkillUsage usage={usage} />);
  const toggle = screen.getByRole("button", { name: /已加载技能 2 个/ });
  await userEvent.click(toggle);
  expect(screen.getByText("weekly-report")).toBeTruthy();
  expect(screen.getByText("自动读取")).toBeTruthy();
  expect(screen.getByText("meeting-notes")).toBeTruthy();
  expect(screen.getByText("手动装配")).toBeTruthy();
});
