// @vitest-environment jsdom

import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
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
const readFile = vi.fn();
const writeFile = vi.fn();
const removeFile = vi.fn();
const archive = vi.fn();
const restore = vi.fn();
const setManaged = vi.fn();

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
    readSkillFile: (id: string, path: string) => readFile(id, path),
    writeSkillFile: (...args: unknown[]) => writeFile(...args),
    removeSkillFile: (...args: unknown[]) => removeFile(...args),
    archiveSkill: (id: string) => archive(id),
    restoreSkill: (id: string) => restore(id),
    setSkillManaged: (id: string, value: boolean) => setManaged(id, value),
  };
});

// 真实编辑器依赖浏览器排版能力，jsdom 下换成文本框：载入时报出“序列化后的”正文，
// 切文件会换 key 重建，正好验证草稿在文件之间来回切换时不会丢。
vi.mock("../components/KbEditor", async () => {
  const { useLayoutEffect } = await import("react");
  return {
    default: ({ initial, onReady, onChange, onInput, reader, label }: {
      initial: string; onReady: (m: string) => void; onChange: (m: string) => void;
      onInput: () => void; reader: { current: (() => string) | null }; label: string;
    }) => {
      useLayoutEffect(() => {
        const node = document.querySelector<HTMLTextAreaElement>(`textarea[aria-label="${label}"]`);
        reader.current = () => node?.value ?? initial;
        onReady(initial);
      }, []); // eslint-disable-line react-hooks/exhaustive-deps
      return (
        <textarea aria-label={label} defaultValue={initial}
          onInput={(event) => { onInput(); onChange(event.currentTarget.value); }} />
      );
    },
  };
});

const App = (await import("../App")).default;

// jsdom 的 File 还没有 text()，上传附件要按它读文本；真实浏览器里是标准方法，只在测试里补。
if (typeof File.prototype.text !== "function") {
  File.prototype.text = function readText(this: File) {
    return new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(this);
    });
  };
}

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
  readFile.mockReset().mockResolvedValue({ path: "references/notes.md", content: "## 备注\n" });
  writeFile.mockReset().mockResolvedValue({ status: "applied", skill: full });
  removeFile.mockReset().mockResolvedValue({ status: "applied", skill: full });
  archive.mockReset().mockResolvedValue(undefined);
  restore.mockReset().mockResolvedValue({ skill_id: "weekly-report", state: "active" });
  setManaged.mockReset().mockResolvedValue({ skill_id: "weekly-report", managed: true });
});

afterEach(cleanup);

test("目录只列名称、说明与时间，点击进入详情", async () => {
  const user = userEvent.setup();
  open();

  expect(await screen.findByText("周报整理")).toBeTruthy();
  expect(screen.getByText("按固定分节整理本周周报")).toBeTruthy();
  // 来源与管理策略不进列表：一个是详情页的元信息，一个是详情页的设置，行里只留可扫描的三项。
  expect(screen.queryByText("管理页手写")).toBeNull();
  expect(screen.queryByText("复盘可改")).toBeNull();

  await user.click(screen.getByRole("button", { name: /周报整理/ }));
  expect(detail).toHaveBeenCalledWith("weekly-report");

  // 详情与资料页同一套骨架：正文是编辑器，没有卡片式面板。
  expect((await screen.findByLabelText("名称") as HTMLInputElement).value).toBe("周报整理");
  expect((screen.getByLabelText("技能正文") as HTMLTextAreaElement).value).toContain("先查日历");
  // 列表页的动作不再出现在详情顶栏：这个位置留给这个对象的操作。
  expect(screen.queryByRole("button", { name: "新建技能" })).toBeNull();
  expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);

  // SKILL.md 的头部是它的 frontmatter：字段照文件里的名字逐行列出，标明这是元数据。
  const meta = screen.getByRole("region", { name: "元数据" });
  for (const key of ["name", "description", "skill_id", "origin"]) {
    expect(within(meta).getByText(key)).toBeTruthy();
  }
  expect(within(meta).getByText("weekly-report")).toBeTruthy();
  expect(within(meta).getByText("由你手写")).toBeTruthy();
  expect((within(meta).getByLabelText("一句话描述") as HTMLInputElement).value).toBe("按固定分节整理本周周报");
  // 元数据下面是标题：名称在块里改，这里是它的呈现。
  expect(screen.getByRole("heading", { level: 1, name: "周报整理" })).toBeTruthy();

  // 三层结构摆在文件树里：正文在根，参考资料与模板是两个目录，空的也列出来。
  const tree = screen.getByRole("navigation", { name: "技能文件" });
  expect(within(tree).getByText("正文")).toBeTruthy();
  expect(within(tree).getByText("参考资料")).toBeTruthy();
  expect(within(tree).getByText("模板")).toBeTruthy();
  expect(within(tree).getByText("notes.md")).toBeTruthy();
  expect(within(tree).getByText("暂无文件")).toBeTruthy();

  // 运行里的事实跟在正文后面，不在读的路上。
  expect(screen.getByText(/最近 7 天未被使用/)).toBeTruthy();
  expect(screen.getByText("后台复盘只能提建议")).toBeTruthy();
});

test("点附件读的是它自己那份内容，改完保存只写这个文件", async () => {
  const user = userEvent.setup();
  open("/skills?id=weekly-report");

  await user.click(await screen.findByRole("button", { name: "notes.md" }));
  expect(readFile).toHaveBeenCalledWith("weekly-report", "references/notes.md");

  const editor = await screen.findByLabelText("references/notes.md 的内容") as HTMLTextAreaElement;
  expect(editor.value).toBe("## 备注\n");
  // 附件没有 frontmatter：元数据块让位给一行路径。
  expect(screen.queryByRole("region", { name: "元数据" })).toBeNull();
  expect(screen.getByText("references/notes.md")).toBeTruthy();
  await user.type(editor, "补一句");
  expect(screen.getByText("未保存")).toBeTruthy();

  await user.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(writeFile).toHaveBeenCalledWith(
    "weekly-report", "references/notes.md", expect.stringContaining("补一句"), "rev-1",
  ));
  // 正文没有动，就不该顺手把 SKILL.md 也重写一遍。
  expect(update).not.toHaveBeenCalled();
  // 保存后停在原来那份文件上：不会退回正文，也不会停在“读取中…”。
  expect(await screen.findByText("已保存")).toBeTruthy();
  expect(await screen.findByLabelText("references/notes.md 的内容")).toBeTruthy();
});

test("删除附件先标记，按保存才真的删", async () => {
  const user = userEvent.setup();
  open("/skills?id=weekly-report");

  await user.click(await screen.findByRole("button", { name: "删除 references/notes.md" }));
  expect(removeFile).not.toHaveBeenCalled();
  expect(screen.getByText("未保存")).toBeTruthy();

  await user.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(removeFile).toHaveBeenCalledWith("weekly-report", "references/notes.md", "rev-1"));
});

test("上传的附件归到选中的那一层，随创建一起提交", async () => {
  const user = userEvent.setup();
  open("/skills?new=1");

  // 页面是懒加载的：第一条查询要等它挂上来，否则拿到的是 Suspense 的占位。
  await user.type(await screen.findByLabelText("标识"), "trip-plan");
  await user.type(screen.getByLabelText("名称"), "行程规划");
  await user.type(screen.getByLabelText("一句话描述"), "规划出行的固定步骤");
  await user.type(screen.getByLabelText("技能正文"), "先定交通，再定住宿");

  // 上传后直接打开新文件，树里立刻能看到它（还没保存，行尾带一个未保存的点）。
  const file = new File(["# 排查表\n"], "check.md", { type: "text/markdown" });
  await user.upload(screen.getByLabelText("添加附件"), file);
  const tree = screen.getByRole("navigation", { name: "技能文件" });
  expect(await within(tree).findByText("check.md")).toBeTruthy();
  expect(await screen.findByLabelText("references/check.md 的内容")).toBeTruthy();

  await user.click(screen.getByRole("button", { name: "创建" }));

  await waitFor(() => expect(create).toHaveBeenCalledWith({
    skill_id: "trip-plan", name: "行程规划", description: "规划出行的固定步骤", body: "先定交通，再定住宿",
    attachments: { "references/check.md": "# 排查表\n" },
  }));
  expect(await screen.findByText("周报整理")).toBeTruthy();
});

test("使用次数按版本汇总在历史版本行上，不再单列一张使用记录", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date("2026-09-24T12:00:00Z"));
  versions.mockResolvedValue([
    {
      revision: "rev-1", created_at: "2026-09-20T08:00:00Z",
      change_id: null, actor: null, reason: null,          // 改启用状态：正文没变
    },
    {
      revision: "rev-1", created_at: "2026-09-21T08:00:00Z",
      change_id: "chg_1", actor: "foreground", reason: "用户要求把排查流程沉淀为技能",
    },
  ]);
  detail.mockResolvedValue({
    ...full,
    usage: [
      { run_id: "r1", skill_id: "weekly-report", revision: "rev-1", source: "auto", loaded_at: "2026-09-23T10:00:00Z" },
      { run_id: "r2", skill_id: "weekly-report", revision: "rev-1", source: "auto", loaded_at: "2026-09-20T10:00:00Z" },
      { run_id: "r3", skill_id: "weekly-report", revision: "rev-1", source: "manual", loaded_at: "2026-09-10T10:00:00Z" },
    ],
  });
  open("/skills?id=weekly-report");

  // 事实行只看最近 7 天；版本行说这一版一共被读过几次。
  expect(await screen.findByText(/最近 7 天被使用 2 次/)).toBeTruthy();
  expect((await screen.findAllByText(/被使用 3 次/)).length).toBe(2);
  expect(screen.getByText("对话里要求")).toBeTruthy();
  // 没有变更记录的提交（改启用状态、改复盘策略）也要说清它是什么。
  expect(screen.getByText("设置变更，正文没有变")).toBeTruthy();
  expect(screen.queryByText("使用记录")).toBeNull();
  expect(screen.queryByText("手动装配")).toBeNull();
  vi.useRealTimers();
});

test("保存冲突（409）提示重新载入，不覆盖", async () => {
  update.mockRejectedValue(new ApiError("skill_conflict", "版本过期", 409));
  const user = userEvent.setup();
  open("/skills?id=weekly-report");

  const body = await screen.findByLabelText("技能正文");
  await user.type(body, "改一下");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(await screen.findByText("技能已被更新")).toBeTruthy();
  expect(screen.getByRole("button", { name: "重新载入（放弃本页修改）" })).toBeTruthy();
  expect(update).toHaveBeenCalledWith("weekly-report", "rev-1", expect.objectContaining({ body: expect.stringContaining("改一下") }));
});

test("归档收在 ⋯ 菜单里，执行后顶栏说明它不再进对话", async () => {
  const user = userEvent.setup();
  open("/skills?id=weekly-report");

  await user.click(await screen.findByRole("button", { name: "更多操作" }));
  await user.click(screen.getByRole("menuitem", { name: "归档" }));

  await waitFor(() => expect(archive).toHaveBeenCalledWith("weekly-report"));
  expect(await screen.findByText("已归档，不进入对话")).toBeTruthy();
  // 事实行里的复盘开关对用户手写的技能不出现：它恒为受保护。
  expect(screen.getByText("后台复盘只能提建议")).toBeTruthy();
});

test("目录行尾的 ⋯ 菜单可以归档，结果一句话留在列表上方", async () => {
  const user = userEvent.setup();
  open();

  const row = (await screen.findByText("周报整理")).closest(".list-item") as HTMLElement;
  await user.click(within(row).getByRole("button", { name: "更多操作" }));
  await user.click(screen.getByRole("menuitem", { name: "归档" }));

  await waitFor(() => expect(archive).toHaveBeenCalledWith("weekly-report"));
  // 提示文案是「已归档」，与标签页同名，用 role 消歧。
  expect((await screen.findByRole("status")).textContent).toBe("已归档");
});

test("归档提示停留 3 秒后自动消失", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup();
  open();

  const row = (await screen.findByText("周报整理")).closest(".list-item") as HTMLElement;
  await user.click(within(row).getByRole("button", { name: "更多操作" }));
  await user.click(screen.getByRole("menuitem", { name: "归档" }));

  expect((await screen.findByRole("status")).textContent).toBe("已归档");
  await act(async () => { vi.advanceTimersByTime(3000); });
  expect(screen.queryByRole("status")).toBeNull();
});

test("已归档目录的行尾菜单改为恢复启用", async () => {
  list.mockImplementation((state: string) => Promise.resolve(
    state === "archived" ? [{ ...summaries[0], state: "archived" as const }] : summaries,
  ));
  const user = userEvent.setup();
  open("/skills?state=archived");

  const row = (await screen.findByText("周报整理")).closest(".list-item") as HTMLElement;
  await user.click(within(row).getByRole("button", { name: "更多操作" }));
  await user.click(screen.getByRole("menuitem", { name: "恢复启用" }));

  await waitFor(() => expect(restore).toHaveBeenCalledWith("weekly-report"));
  expect(await screen.findByText("已恢复")).toBeTruthy();
});

test("待审变更可批准：带建议时的版本提交，驳回只传标识", async () => {
  changes.mockResolvedValue([{
    id: "chg_1",
    review_job_id: null,
    skill_id: "weekly-report",
    action: "patch",
    payload: { body: "复盘建议的正文\n" },
    base_revision: "rev-1",
    reason: "复盘发现步骤可以更稳。后续验证有效。",
    evidence_item_ids: [],
    actor: "review",
    status: "proposed",
    created_at: "2026-09-22T03:00:00Z",
    applied_at: null,
  }]);
  const user = userEvent.setup();
  open();

  expect(await screen.findByText("待确认的技能变更")).toBeTruthy();
  expect(screen.getByText("复盘发现步骤可以更稳。后续验证有效。")).toBeTruthy();
  expect(screen.queryByText("新增内容摘录")).toBeNull();
  expect(screen.queryByText("完整说明")).toBeNull();
  const diffToggle = await screen.findByText("查看完整差异");
  const diff = diffToggle.closest("details") as HTMLDetailsElement;
  expect(diff?.open).toBe(false);
  await user.click(diffToggle);
  expect(diff?.open).toBe(true);

  await user.click(screen.getByRole("button", { name: "批准" }));
  await waitFor(() => expect(approve).toHaveBeenCalledWith("chg_1", "rev-1"));

  await user.click(screen.getByRole("button", { name: "驳回" }));
  await waitFor(() => expect(reject).toHaveBeenCalledWith("chg_1"));
});
