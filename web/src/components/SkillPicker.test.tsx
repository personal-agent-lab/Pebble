// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, expect, test, vi, type Mock } from "vitest";

import type { SkillSelection, SkillSummary } from "../api";
import SkillPicker from "./SkillPicker";

const skills: SkillSummary[] = [
  {
    skill_id: "weekly-report", name: "周报整理", description: "按固定分节整理本周周报",
    origin: "user", managed: false, state: "active", revision: "r1", updated_at: "2026-09-22T01:00:00Z",
    last_loaded_at: null,
  },
  {
    skill_id: "meeting-notes", name: "会议纪要", description: "固定格式的会议纪要",
    origin: "explicit", managed: true, state: "active", revision: "r2", updated_at: "2026-09-22T02:00:00Z",
    last_loaded_at: null,
  },
];

const DEFAULT: SkillSelection = { skills: [], excluded_skill_ids: [], auto_match: true };

/** 受控组件：父组件保存选择，连续点击按累积后的状态计算，与输入框里的真实用法一致。 */
function Host({ selection: initial, onChange }: { selection: SkillSelection; onChange: Mock }) {
  const [selection, setSelection] = useState(initial);
  return (
    <SkillPicker skills={skills} selection={selection}
      onChange={(next) => { onChange(next); setSelection(next); }} />
  );
}

afterEach(cleanup);

test("三态选择互斥：加入后排除会清掉加入，反之亦然", async () => {
  const onChange = vi.fn();
  render(<Host selection={DEFAULT} onChange={onChange} />);
  await userEvent.click(screen.getByRole("button", { name: "选择技能" }));

  await userEvent.click(screen.getAllByRole("menuitemcheckbox", { name: "加入" })[0]);
  expect(onChange).toHaveBeenLastCalledWith({
    skills: [{ id: "weekly-report" }], excluded_skill_ids: [], auto_match: true,
  });

  await userEvent.click(screen.getAllByRole("menuitemcheckbox", { name: "排除" })[0]);
  expect(onChange).toHaveBeenLastCalledWith({
    skills: [], excluded_skill_ids: ["weekly-report"], auto_match: true,
  });

  await userEvent.click(screen.getAllByRole("menuitemcheckbox", { name: "加入" })[1]);
  expect(onChange).toHaveBeenLastCalledWith({
    skills: [{ id: "meeting-notes" }], excluded_skill_ids: ["weekly-report"], auto_match: true,
  });
});

test("自动匹配可关闭，重置回到默认", async () => {
  const onChange = vi.fn();
  render(
    <Host
      selection={{ skills: [{ id: "meeting-notes" }], excluded_skill_ids: ["weekly-report"], auto_match: false }}
      onChange={onChange}
    />,
  );
  await userEvent.click(screen.getByRole("button", { name: "选择技能" }));

  // 已有选择时按钮亮起并显示计数。
  expect(screen.getByText("1")).toBeTruthy();

  await userEvent.click(screen.getByRole("button", { name: "重置" }));
  expect(onChange).toHaveBeenLastCalledWith(DEFAULT);

  await userEvent.click(screen.getByLabelText("自动匹配"));
  expect(onChange).toHaveBeenLastCalledWith({ ...DEFAULT, auto_match: false });
});

test("没有技能时给出空态说明", async () => {
  render(<SkillPicker skills={[]} selection={DEFAULT} onChange={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "选择技能" }));
  expect(screen.getByText(/还没有启用中的技能/)).toBeTruthy();
});
