// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApiError, type SkillDetail, type SkillSummary } from "../api";

const list = vi.fn();
const detail = vi.fn();
const create = vi.fn();
const update = vi.fn();
const changes = vi.fn();
const approve = vi.fn();
const reject = vi.fn();
const versions = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve([]),
    listSkills: (...args: unknown[]) => list(...args),
    getSkill: (id: string) => detail(id),
    createSkill: (fields: unknown) => create(fields),
    updateSkill: (...args: unknown[]) => update(...args),
    listSkillChanges: (status?: string) => changes(status),
    approveSkillChange: (...args: unknown[]) => approve(...args),
    rejectSkillChange: (id: string) => reject(id),
    listSkillVersions: () => versions(),
  };
});

const App = (await import("../App")).default;

const open = (path = "/skills") =>
  render(<MemoryRouter initialEntries={[path]}><App /></MemoryRouter>);

const summaries: SkillSummary[] = [
  {
    skill_id: "weekly-report", name: "周报整理", description: "按固定分节整理本周周报",
    origin: "user", managed: false, state: "active", revision: "rev-1",
    updated_at: "2026-09-22T01:00:00Z", last_loaded_at: null,
  },
  {
    skill_id: "meeting-notes", name: "会议纪要", description: "固定格式",
    origin: "review", managed: true, state: "active", revision: "rev-2",
    updated_at: "2026-09-22T02:00:00Z", last_loaded_at: null,
  },
];

const full: SkillDetail = {
  ...summaries[0],
  created_at: "2026-09-20T00:00:00Z",
  body: "## 步骤\n\n1. 先查日历\n",
  files: [{ path: "references/notes.md", hash: "h1" }],
  usage: [],
};

beforeEach(() => {
  list.mockReset().mockResolvedValue(summaries);
  detail.mockReset().mockResolvedValue(full);
  create.mockReset().mockResolvedValue({ status: "applied", skill: full });
  update.mockReset().mockResolvedValue({ status: "applied", skill: full });
  changes.mockReset().mockResolvedValue([]);
  approve.mockReset().mockResolvedValue({ status: "applied" });
  reject.mockReset().mockResolvedValue(undefined);
  versions.mockReset().mockResolvedValue([]);
});

afterEach(cleanup);

test("目录列出技能与来源标记，点击进入详情", async () => {
  const user = userEvent.setup();
  open();

  expect(await screen.findByText("周报整理")).toBeTruthy();
  expect(screen.getByText("管理页手写")).toBeTruthy();
  expect(screen.getByText("复盘可改")).toBeTruthy();

  await user.click(screen.getByRole("button", { name: /周报整理/ }));
  expect(detail).toHaveBeenCalledWith("weekly-report");
  // 正文在编辑框里，内容取 value 而非文本节点。
  expect(((await screen.findByLabelText("正文")) as HTMLTextAreaElement).value).toContain("先查日历");
  expect(screen.getByText("references/notes.md")).toBeTruthy();
  expect(screen.getByText("版本 rev-1")).toBeTruthy();
  expect(screen.getByRole("button", { name: "返回目录" })).toBeTruthy();
});

test("创建表单填齐后提交，成功后回到目录", async () => {
  const user = userEvent.setup();
  open("/skills?new=1");

  await user.type(screen.getByLabelText("标识"), "trip-plan");
  await user.type(screen.getByLabelText("名称"), "行程规划");
  await user.type(screen.getByLabelText("一句话描述"), "规划出行的固定步骤");
  await user.type(screen.getByLabelText("正文"), "先定交通，再定住宿");
  await user.click(screen.getByRole("button", { name: "创建" }));

  await waitFor(() => expect(create).toHaveBeenCalledWith({
    skill_id: "trip-plan", name: "行程规划", description: "规划出行的固定步骤", body: "先定交通，再定住宿",
  }));
  expect(await screen.findByText("周报整理")).toBeTruthy();
});

test("保存冲突（409）提示重新载入，不覆盖", async () => {
  update.mockRejectedValue(new ApiError("skill_conflict", "版本过期", 409));
  const user = userEvent.setup();
  open("/skills?id=weekly-report");

  const body = await screen.findByLabelText("正文");
  await user.type(body, "改一下");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(await screen.findByText("技能已被更新")).toBeTruthy();
  expect(screen.getByRole("button", { name: "重新载入（放弃本页修改）" })).toBeTruthy();
  expect(update).toHaveBeenCalledWith("weekly-report", "rev-1", expect.objectContaining({ body: expect.stringContaining("改一下") }));
});

test("待审变更可批准：带建议时的版本提交，驳回只传标识", async () => {
  changes.mockResolvedValue([{
    id: "chg_1",
    review_job_id: null,
    skill_id: "weekly-report",
    action: "patch",
    payload: { body: "复盘建议的正文\n" },
    base_revision: "rev-1",
    reason: "复盘发现步骤可以更稳",
    evidence_item_ids: [],
    actor: "review",
    status: "proposed",
    created_at: "2026-09-22T03:00:00Z",
    applied_at: null,
  }]);
  const user = userEvent.setup();
  open();

  expect(await screen.findByText("待确认的技能变更")).toBeTruthy();
  expect(screen.getByText("复盘发现步骤可以更稳")).toBeTruthy();
  expect(screen.getByText("复盘建议的正文")).toBeTruthy();

  await user.click(screen.getByRole("button", { name: "批准" }));
  await waitFor(() => expect(approve).toHaveBeenCalledWith("chg_1", "rev-1"));

  await user.click(screen.getByRole("button", { name: "驳回" }));
  await waitFor(() => expect(reject).toHaveBeenCalledWith("chg_1"));
});
