// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApiError, type KbDocument } from "../api";

const PATH = "kb/项目/验收.md";
const FOLDER = "项目";
let current: KbDocument;
const update = vi.fn();
const create = vi.fn();
const remove = vi.fn();
const move = vi.fn();
const draft = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve([]),
    listKbDocuments: () => Promise.resolve({ directory: "kb", documents: [] }),
    listKbFolders: () => Promise.resolve({ folders: ["项目", "归档"] }),
    getKbDocument: () => Promise.resolve(current),
    updateKbDocument: (...args: unknown[]) => update(...args),
    createKbDocument: (...args: unknown[]) => create(...args),
    deleteKbDocument: (...args: unknown[]) => remove(...args),
    moveKbDocument: (...args: unknown[]) => move(...args),
    draftKbSummary: (...args: unknown[]) => draft(...args),
  };
});

// 真实编辑器依赖浏览器排版能力，jsdom 下换成文本框：载入时报告“序列化后的”正文，
// 模拟编辑器把原文重新排版（列表符号从 * 变成 -），验证只打开不会触发保存。
vi.mock("../components/KbEditor", async () => {
  const { useEffect } = await import("react");
  return {
    default: ({ initial, onReady, onChange, onInput, reader, label }: {
      initial: string; onReady: (m: string) => void; onChange: (m: string) => void;
      onInput: () => void; reader: { current: (() => string) | null }; label: string;
    }) => {
      const normalized = initial.replace(/^\* /gm, "- ");
      useEffect(() => {
        const node = document.querySelector<HTMLTextAreaElement>(`textarea[aria-label="${label}"]`);
        reader.current = () => node?.value ?? normalized;
        onReady(normalized);
      }, []); // eslint-disable-line react-hooks/exhaustive-deps
      // 与真实编辑器一致：输入立刻通知，内容变更通知不及时（这里干脆不发），保存要靠 reader 取值。
      void onChange;
      return <textarea aria-label={label} defaultValue={normalized} onInput={onInput} />;
    },
  };
});

const App = (await import("../App")).default;

function Where() {
  const location = useLocation();
  return <div data-testid="where">{location.pathname + location.search}</div>;
}

const open = async (path = `/kb/doc?path=${encodeURIComponent(PATH)}`) => {
  render(<MemoryRouter initialEntries={[path]}><App /><Where /></MemoryRouter>);
};

beforeEach(() => {
  current = {
    id: "kb_1", path: PATH, title: "验收纪要", summary: "二期验收结论",
    created_at: "2026-09-14T01:00:00Z", updated_at: "2026-09-14T01:00:00Z",
    version: "c1", body: "## 结果\n\n* 通过",
  };
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

test("只打开不编辑不能保存，编辑器的重新排版不算改动", async () => {
  await open();
  expect(await screen.findByDisplayValue("验收纪要")).toBeTruthy();
  expect(screen.getByDisplayValue("二期验收结论")).toBeTruthy();
  expect(screen.getByText("kb_1")).toBeTruthy();
  expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
});

test("改了标题就带着读取时的版本保存，正文未改时原样提交原文，存完回到所在文件夹", async () => {
  update.mockResolvedValue({ id: "kb_1", path: PATH, title: "验收纪要（二期）", version: "c2", index_status: "ok" });
  const user = userEvent.setup();
  await open();
  const title = await screen.findByDisplayValue("验收纪要");

  await user.type(title, "（二期）");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(update).toHaveBeenCalledWith(PATH, "c1", {
    title: "验收纪要（二期）", summary: "二期验收结论", body: "## 结果\n\n* 通过",
  });
  await waitFor(() => expect(screen.getByTestId("where").textContent).toBe(`/kb?dir=${encodeURIComponent(FOLDER)}`));
});

test("保存后暂不可检索时留在本页提示", async () => {
  update.mockResolvedValue({ id: "kb_1", path: PATH, title: "验收纪要（二期）", version: "c2", index_status: "stale" });
  const user = userEvent.setup();
  await open();
  await user.type(await screen.findByDisplayValue("验收纪要"), "（二期）");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(await screen.findByText("已保存，但当前不可检索")).toBeTruthy();
  expect(screen.getByTestId("where").textContent).toBe(`/kb/doc?path=${encodeURIComponent(PATH)}`);
});

test("正文改过就提交编辑器里的内容；版本冲突时提示并可重新载入", async () => {
  update.mockRejectedValue(new ApiError("version_conflict", "当前版本为 c9", 409, "c9"));
  const user = userEvent.setup();
  await open();
  const editor = await screen.findByLabelText("资料正文");

  await user.type(editor, "，另有两项遗留");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(update.mock.calls[0][2].body).toBe("## 结果\n\n- 通过，另有两项遗留");
  expect(await screen.findByText("资料已被修改")).toBeTruthy();
  current = { ...current, version: "c9", body: "## 结果\n\n* 别人改过" };
  await user.click(screen.getByRole("button", { name: /重新载入/ }));
  await waitFor(() =>
    expect((screen.getByLabelText("资料正文") as HTMLTextAreaElement).value).toBe("## 结果\n\n- 别人改过"),
  );
  expect(screen.queryByText("资料已被修改")).toBeNull();
});

test("操作收在顶栏的“⋯”菜单里，有未保存的修改时不可用", async () => {
  const user = userEvent.setup();
  await open();
  const title = await screen.findByDisplayValue("验收纪要");
  expect(screen.queryByRole("button", { name: "删除" })).toBeNull();

  await user.type(title, "（改）");
  await user.click(screen.getByRole("button", { name: "更多操作" }));
  for (const name of ["重命名", "移动", "删除"]) {
    expect((screen.getByRole("menuitem", { name }) as HTMLButtonElement).disabled).toBe(true);
  }
  expect(screen.getByText("先保存或放弃修改")).toBeTruthy();
});

test("顶栏只显示所在文件夹，不显示文件名", async () => {
  await open();
  await screen.findByDisplayValue("验收纪要");
  const where = screen.getByRole("navigation", { name: "所在位置" });
  expect(where.textContent).toBe("资料库/项目");
});

test("删除在对话框里确认，确认后回到资料所在的文件夹", async () => {
  remove.mockResolvedValue({ path: PATH });
  const user = userEvent.setup();
  await open();
  await screen.findByDisplayValue("验收纪要");

  await user.click(screen.getByRole("button", { name: "更多操作" }));
  await user.click(screen.getByRole("menuitem", { name: "删除" }));
  expect(remove).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog", { name: "删除资料？" }).textContent).toContain("《验收纪要》");
  await user.click(screen.getByRole("button", { name: "删除" }));

  expect(remove).toHaveBeenCalledWith(PATH, "c1");
  await waitFor(() => expect(screen.getByTestId("where").textContent).toBe(`/kb?dir=${encodeURIComponent(FOLDER)}`));
});

test("重命名改的是标题，文件名跟着变时跳到新地址", async () => {
  update.mockResolvedValue({ id: "kb_1", path: "kb/项目/二期验收.md", title: "二期验收", version: "c2" });
  const user = userEvent.setup();
  await open();
  await screen.findByDisplayValue("验收纪要");

  await user.click(screen.getByRole("button", { name: "更多操作" }));
  await user.click(screen.getByRole("menuitem", { name: "重命名" }));
  const input = screen.getByLabelText("资料名称") as HTMLInputElement;
  expect(input.value).toBe("验收纪要");
  const submit = screen.getByRole("button", { name: "重命名" }) as HTMLButtonElement;
  expect(submit.disabled).toBe(true);
  await user.clear(input);
  await user.type(input, "二期验收");
  await user.click(submit);

  expect(update).toHaveBeenCalledWith(PATH, "c1", { title: "二期验收" });
  await waitFor(() =>
    expect(screen.getByTestId("where").textContent).toBe(`/kb/doc?path=${encodeURIComponent("kb/项目/二期验收.md")}`),
  );
});

test("⌘S 保存改了标题的资料后留在本页，换到文件名跟随后的新地址", async () => {
  update.mockResolvedValue({ id: "kb_1", path: "kb/项目/验收纪要（二期）.md", title: "验收纪要（二期）", version: "c2", index_status: "ok" });
  const user = userEvent.setup();
  await open();
  await user.type(await screen.findByDisplayValue("验收纪要"), "（二期）");
  await user.keyboard("{Meta>}s{/Meta}");

  await waitFor(() =>
    expect(screen.getByTestId("where").textContent)
      .toBe(`/kb/doc?path=${encodeURIComponent("kb/项目/验收纪要（二期）.md")}`),
  );
  expect(await screen.findByText("已保存")).toBeTruthy();
});

test("移动时逐层选择文件夹，文件名沿用原名，移完跳到新位置", async () => {
  move.mockResolvedValue({ id: "kb_1", path: "kb/归档/验收.md", previous_path: PATH, title: "验收纪要", version: "c2" });
  const user = userEvent.setup();
  await open();
  await screen.findByDisplayValue("验收纪要");

  await user.click(screen.getByRole("button", { name: "更多操作" }));
  await user.click(screen.getByRole("menuitem", { name: "移动" }));
  const here = screen.getByRole("button", { name: "移到这里" }) as HTMLButtonElement;
  expect(here.disabled).toBe(true);
  await user.click(screen.getByRole("button", { name: "资料库" }));
  await user.click(await screen.findByRole("button", { name: "归档" }));
  expect(here.disabled).toBe(false);
  await user.click(here);

  expect(move).toHaveBeenCalledWith(PATH, "c1", "归档/验收.md");
  await waitFor(() =>
    expect(screen.getByTestId("where").textContent).toBe(`/kb/doc?path=${encodeURIComponent("kb/归档/验收.md")}`),
  );
});

test("新建资料需要标题，保存到进入时所在的文件夹后回到该文件夹", async () => {
  create.mockResolvedValue({ id: "kb_9", path: "kb/课程/lab1.md", title: "实验一", version: "c1" });
  const user = userEvent.setup();
  await open(`/kb/new?dir=${encodeURIComponent("课程")}`);
  expect((await screen.findByRole("navigation", { name: "所在位置" })).textContent).toBe("保存到：资料库/课程");
  const save = await screen.findByRole("button", { name: "保存" });
  await user.type(screen.getByLabelText("资料正文"), "提交截止 10 月 8 日");
  expect((save as HTMLButtonElement).disabled).toBe(true);

  await user.type(screen.getByLabelText("资料标题"), "实验一");
  await user.type(screen.getByPlaceholderText("摘要"), "实验要求与截止日期");
  await user.click(save);

  expect(create).toHaveBeenCalledWith({
    title: "实验一", summary: "实验要求与截止日期", body: "提交截止 10 月 8 日", directory: "课程",
  });
  await waitFor(() =>
    expect(screen.getByTestId("where").textContent).toBe(`/kb?dir=${encodeURIComponent("课程")}`),
  );
});

test("标题或正文为空时不能保存", async () => {
  const user = userEvent.setup();
  await open("/kb/new");
  const save = await screen.findByRole("button", { name: "保存" }) as HTMLButtonElement;

  await user.type(screen.getByLabelText("资料标题"), "只有标题");
  expect(save.disabled).toBe(true);
  await user.type(screen.getByLabelText("资料正文"), "正文");
  expect(save.disabled).toBe(false);
  await user.clear(screen.getByLabelText("资料正文"));
  expect(save.disabled).toBe(true);

  await user.type(screen.getByLabelText("资料正文"), "正文");
  await user.clear(screen.getByLabelText("资料标题"));
  expect(save.disabled).toBe(true);
  expect(create).not.toHaveBeenCalled();
});

test("已有资料清空标题或正文后也不能保存", async () => {
  const user = userEvent.setup();
  await open();
  const title = await screen.findByDisplayValue("验收纪要");
  const save = screen.getByRole("button", { name: "保存" }) as HTMLButtonElement;

  await user.clear(title);
  expect(save.disabled).toBe(true);
  await user.type(title, "验收纪要（改）");
  expect(save.disabled).toBe(false);
  await user.clear(screen.getByLabelText("资料正文"));
  expect(save.disabled).toBe(true);
  expect(update).not.toHaveBeenCalled();
});

test("在根目录新建时保存到资料库根目录", async () => {
  create.mockResolvedValue({ id: "kb_9", path: "kb/笔记-abc123.md", title: "笔记", version: "c1" });
  const user = userEvent.setup();
  await open("/kb/new");
  expect((await screen.findByRole("navigation", { name: "所在位置" })).textContent).toBe("保存到：资料库");
  await user.type(screen.getByLabelText("资料标题"), "笔记");
  await user.type(screen.getByLabelText("资料正文"), "内容");
  await user.click(screen.getByRole("button", { name: "保存" }));
  expect(create.mock.calls[0][0].directory).toBe("");
});

test("输入后马上保存也能拿到最新正文；改了又改回去不产生写入", async () => {
  const user = userEvent.setup();
  await open();
  const editor = await screen.findByLabelText("资料正文");

  await user.type(editor, "X");
  await user.type(editor, "{Backspace}");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(update).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByTestId("where").textContent).toBe(`/kb?dir=${encodeURIComponent(FOLDER)}`));
});

test("只改说明也能保存，说明原样提交", async () => {
  update.mockResolvedValue({ id: "kb_1", path: PATH, title: "验收纪要", version: "c2", index_status: "ok" });
  const user = userEvent.setup();
  await open();
  const summary = await screen.findByDisplayValue("二期验收结论");

  await user.clear(summary);
  await user.type(summary, "二期验收结论与遗留问题");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(update.mock.calls[0][2]).toEqual({
    title: "验收纪要", summary: "二期验收结论与遗留问题", body: "## 结果\n\n* 通过",
  });
});

test("生成说明：按当前标题与正文起草并填入，需要正文才能生成", async () => {
  draft.mockResolvedValue({ summary: "青铜项目二期排期" });
  const user = userEvent.setup();
  await open(`/kb/new?dir=${encodeURIComponent(FOLDER)}`);

  const generate = await screen.findByRole("button", { name: /生成/ });
  expect((generate as HTMLButtonElement).disabled).toBe(true);

  await user.type(screen.getByLabelText("资料标题"), "青铜周会");
  await user.type(screen.getByLabelText("资料正文"), "二期排期定在十月");
  await user.click(generate);

  expect(draft).toHaveBeenCalledWith("青铜周会", "二期排期定在十月");
  expect(await screen.findByDisplayValue("青铜项目二期排期")).toBeTruthy();
  expect(create).not.toHaveBeenCalled();
});
