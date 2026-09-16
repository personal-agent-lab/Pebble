// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApiError, type KbDocument } from "../api";

const PATH = "kb/项目/验收.md";
let current: KbDocument;
const update = vi.fn();
const create = vi.fn();
const remove = vi.fn();
const move = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve([]),
    listKbDocuments: () => Promise.resolve({ directory: "kb", documents: [] }),
    getKbDocument: () => Promise.resolve(current),
    updateKbDocument: (...args: unknown[]) => update(...args),
    createKbDocument: (...args: unknown[]) => create(...args),
    deleteKbDocument: (...args: unknown[]) => remove(...args),
    moveKbDocument: (...args: unknown[]) => move(...args),
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
    id: "kb_1", path: PATH, title: "验收纪要", summary: "二期验收结论", tags: ["项目"],
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
  expect(screen.getByDisplayValue("项目")).toBeTruthy();
  expect(screen.getByText("kb_1")).toBeTruthy();
  expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);
});

test("改了标签就带着读取时的版本保存，正文未改时原样提交原文", async () => {
  update.mockResolvedValue({ id: "kb_1", path: PATH, title: "验收纪要", version: "c2", index_status: "ok" });
  const user = userEvent.setup();
  await open();
  const tags = await screen.findByDisplayValue("项目");

  await user.clear(tags);
  await user.type(tags, "项目，验收");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(update).toHaveBeenCalledWith(PATH, "c1", {
    title: "验收纪要", summary: "二期验收结论", body: "## 结果\n\n* 通过", tags: ["项目", "验收"],
  });
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

test("删除先确认，确认后回到资料列表", async () => {
  remove.mockResolvedValue({ path: PATH });
  const user = userEvent.setup();
  await open();
  await screen.findByDisplayValue("验收纪要");

  await user.click(screen.getByRole("button", { name: "删除" }));
  expect(remove).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "确认删除" }));

  expect(remove).toHaveBeenCalledWith(PATH, "c1");
  await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/kb"));
});

test("移动后跳到新位置", async () => {
  move.mockResolvedValue({ id: "kb_1", path: "kb/归档/验收.md", previous_path: PATH, title: "验收纪要", version: "c2" });
  const user = userEvent.setup();
  await open();
  await screen.findByDisplayValue("验收纪要");

  await user.click(screen.getByRole("button", { name: "移动或重命名" }));
  const target = screen.getByLabelText("新位置");
  await user.clear(target);
  await user.type(target, "归档/验收.md");
  await user.click(screen.getByRole("button", { name: "移动" }));

  expect(move).toHaveBeenCalledWith(PATH, "c1", "归档/验收.md");
  await waitFor(() =>
    expect(screen.getByTestId("where").textContent).toBe(`/kb/doc?path=${encodeURIComponent("kb/归档/验收.md")}`),
  );
});

test("新建资料需要标题，保存后进入新资料", async () => {
  create.mockResolvedValue({ id: "kb_9", path: "kb/课程/lab1.md", title: "实验一", version: "c1" });
  const user = userEvent.setup();
  await open("/kb/new");
  const save = await screen.findByRole("button", { name: "保存" });
  await user.type(screen.getByLabelText("资料正文"), "提交截止 10 月 8 日");
  expect((save as HTMLButtonElement).disabled).toBe(true);

  await user.type(screen.getByLabelText("资料标题"), "实验一");
  await user.type(screen.getByPlaceholderText(/一句话说明/), "实验要求与截止日期");
  await user.type(screen.getByPlaceholderText(/可选，例如/), "课程/lab1");
  await user.click(save);

  expect(create).toHaveBeenCalledWith({
    title: "实验一", summary: "实验要求与截止日期", body: "提交截止 10 月 8 日", tags: [], path: "课程/lab1",
  });
  await waitFor(() =>
    expect(screen.getByTestId("where").textContent).toBe(`/kb/doc?path=${encodeURIComponent("kb/课程/lab1.md")}`),
  );
});

test("输入后马上保存也能拿到最新正文；改了又改回去不产生写入", async () => {
  const user = userEvent.setup();
  await open();
  const editor = await screen.findByLabelText("资料正文");

  await user.type(editor, "X");
  await user.type(editor, "{Backspace}");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(update).not.toHaveBeenCalled();
  expect(await screen.findByText("没有需要保存的改动")).toBeTruthy();
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
    title: "验收纪要", summary: "二期验收结论与遗留问题", body: "## 结果\n\n* 通过", tags: ["项目"],
  });
});
