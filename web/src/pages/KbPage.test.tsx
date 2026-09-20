// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApiError } from "../api";

const search = vi.fn();
const createFolder = vi.fn();
const update = vi.fn();
const move = vi.fn();
const remove = vi.fn();
let folders: string[] = [];

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve([]),
    listKbDocuments: () => Promise.resolve({
      directory: "kb",
      documents: [
        { id: "kb_1", path: "kb/项目/星云验收.md", title: "星云验收纪要", version: "c1", updated_at: "2026-09-14T01:00:00Z" },
        { id: "kb_2", path: "kb/inbox/周会.md", title: "周会纪要", version: "c2", updated_at: "2026-09-13T01:00:00Z" },
        { id: "kb_3", path: "kb/说明.md", title: "资料说明", summary: "怎么用资料库", version: "c3", updated_at: "2026-09-12T01:00:00Z" },
      ],
    }),
    listKbFolders: () => Promise.resolve({ folders }),
    createKbFolder: (path: string) => createFolder(path),
    updateKbDocument: (...args: unknown[]) => update(...args),
    moveKbDocument: (...args: unknown[]) => move(...args),
    deleteKbDocument: (...args: unknown[]) => remove(...args),
    searchKb: (q: string) => search(q),
  };
});

const App = (await import("../App")).default;

function Where() {
  const location = useLocation();
  return <div data-testid="where">{location.pathname + location.search}</div>;
}

const open = (path = "/kb") =>
  render(<MemoryRouter initialEntries={[path]}><App /><Where /></MemoryRouter>);

beforeEach(() => {
  folders = ["inbox", "项目"];
});

afterEach(() => {
  cleanup();
  search.mockReset();
  createFolder.mockReset();
  update.mockReset();
  move.mockReset();
  remove.mockReset();
});

test("根目录只列第一层：文件夹在前，根目录资料在后", async () => {
  open();

  const doc = await screen.findByRole("link", { name: /资料说明/ });
  expect(doc.getAttribute("href")).toBe(`/kb/doc?path=${encodeURIComponent("kb/说明.md")}`);
  expect(screen.getByRole("link", { name: /项目/ }).getAttribute("href")).toBe(`/kb?dir=${encodeURIComponent("项目")}`);
  expect(screen.getByRole("link", { name: /inbox/ })).toBeTruthy();
  expect(screen.queryByRole("link", { name: /星云验收纪要/ })).toBeNull();
  expect(screen.getByText("3 份资料")).toBeTruthy();
});

test("点进文件夹列出其中资料，面包屑回到根目录", async () => {
  const user = userEvent.setup();
  open();

  await user.click(await screen.findByRole("link", { name: /项目/ }));
  expect(await screen.findByRole("link", { name: /星云验收纪要/ })).toBeTruthy();
  expect(screen.queryByRole("link", { name: /资料说明/ })).toBeNull();

  await user.click(screen.getByRole("link", { name: "资料库" }));
  expect(await screen.findByRole("link", { name: /资料说明/ })).toBeTruthy();
  expect(screen.getByTestId("where").textContent).toBe("/kb");
});

test("子文件夹以文件夹名为标题，面包屑只列上级，返回按钮回到上一级", async () => {
  const user = userEvent.setup();
  open(`/kb?dir=${encodeURIComponent("项目/星云")}`);

  expect((await screen.findByRole("heading", { level: 2 })).textContent).toBe("星云");
  const where = screen.getByRole("navigation", { name: "当前位置" });
  expect(where.textContent).toContain("资料库/项目");
  expect(where.textContent).not.toContain("星云");

  await user.click(screen.getByRole("link", { name: "返回上一级" }));
  await waitFor(() => expect(screen.getByTestId("where").textContent).toBe(`/kb?dir=${encodeURIComponent("项目")}`));
  expect((await screen.findByRole("heading", { level: 2 })).textContent).toBe("项目");

  await user.click(screen.getByRole("link", { name: "返回上一级" }));
  await waitFor(() => expect(screen.getByTestId("where").textContent).toBe("/kb"));
  expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("资料");
  expect(screen.queryByRole("link", { name: "返回上一级" })).toBeNull();
});

test("新建菜单：文档进入带当前文件夹的新建页", async () => {
  const user = userEvent.setup();
  open(`/kb?dir=${encodeURIComponent("项目")}`);
  await screen.findByRole("link", { name: /星云验收纪要/ });

  await user.click(screen.getByRole("button", { name: /新建/ }));
  await user.click(screen.getByRole("menuitem", { name: /Markdown 文档/ }));

  await waitFor(() =>
    expect(screen.getByTestId("where").textContent).toBe(`/kb/new?dir=${encodeURIComponent("项目")}`),
  );
});

test("新建菜单：文件夹建在当前文件夹下，建好后出现在列表里", async () => {
  createFolder.mockImplementation((path: string) => {
    folders = [...folders, path];
    return Promise.resolve({ path: `kb/${path}` });
  });
  const user = userEvent.setup();
  open(`/kb?dir=${encodeURIComponent("项目")}`);
  await screen.findByRole("link", { name: /星云验收纪要/ });

  await user.click(screen.getByRole("button", { name: /新建/ }));
  await user.click(screen.getByRole("menuitem", { name: /文件夹/ }));
  const dialog = screen.getByRole("dialog", { name: "新建文件夹" });
  expect(dialog.textContent).toContain("创建在：项目");
  expect((screen.getByRole("button", { name: "创建" }) as HTMLButtonElement).disabled).toBe(true);
  await user.type(screen.getByLabelText("文件夹名称"), "二期");
  await user.click(screen.getByRole("button", { name: "创建" }));

  expect(createFolder).toHaveBeenCalledWith("项目/二期");
  const created = await screen.findByRole("link", { name: /二期/ });
  expect(created.textContent).toContain("空文件夹");
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("新建文件夹对话框：失败原因留在框里，Esc 与取消都能关闭", async () => {
  createFolder.mockRejectedValue(
    new ApiError("invalid_kb", "资料校验失败", 422, undefined, undefined, [{ field: "path", message: "已有同名文件夹或资料" }]),
  );
  const user = userEvent.setup();
  open();
  await screen.findByRole("link", { name: /资料说明/ });

  await user.click(screen.getByRole("button", { name: /新建/ }));
  await user.click(screen.getByRole("menuitem", { name: /文件夹/ }));
  await user.type(screen.getByLabelText("文件夹名称"), "项目{Enter}");
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "已有同名文件夹或资料");
  expect(createFolder).toHaveBeenCalledWith("项目");

  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();

  await user.click(screen.getByRole("button", { name: /新建/ }));
  await user.click(screen.getByRole("menuitem", { name: /文件夹/ }));
  await user.click(screen.getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("不存在的文件夹给出提示", async () => {
  open(`/kb?dir=${encodeURIComponent("没有")}`);
  expect(await screen.findByText("文件夹不存在")).toBeTruthy();
});

test("输入搜索词后显示命中片段，清空后回到当前文件夹", async () => {
  search.mockResolvedValue({
    query: "NEBULA",
    results: [{ id: "kb_1", path: "kb/项目/星云验收.md", title: "星云验收纪要", heading: "星云验收纪要 / 结果", snippet: "代号 NEBULA-3390" }],
  });
  const user = userEvent.setup();
  open(`/kb?dir=${encodeURIComponent("inbox")}`);
  await screen.findByRole("link", { name: /周会纪要/ });

  await user.type(screen.getByLabelText("搜索资料"), "NEBULA");

  expect(await screen.findByText("代号 NEBULA-3390")).toBeTruthy();
  expect(search).toHaveBeenCalledWith("NEBULA");
  expect(screen.queryByRole("link", { name: /周会纪要/ })).toBeNull();

  await user.clear(screen.getByLabelText("搜索资料"));
  await waitFor(() => expect(screen.queryByText("代号 NEBULA-3390")).toBeNull());
  expect(await screen.findByRole("link", { name: /周会纪要/ })).toBeTruthy();
  expect(screen.getByTestId("where").textContent).toBe(`/kb?dir=${encodeURIComponent("inbox")}`);
});

test("搜索结果只显示所在文件夹与标题，不显示文件名", async () => {
  search.mockResolvedValue({
    query: "NEBULA",
    results: [{ id: "kb_1", path: "kb/项目/星云验收-ab12cd.md", title: "星云验收纪要", heading: "结果", snippet: "代号 NEBULA-3390" }],
  });
  const user = userEvent.setup();
  open();
  await user.type(await screen.findByLabelText("搜索资料"), "NEBULA");

  expect(await screen.findByText("资料库 / 项目 / 星云验收纪要")).toBeTruthy();
  expect(screen.queryByText(/ab12cd/)).toBeNull();
});

test("资料行的“⋯”菜单：重命名改标题，完成后提示并刷新列表", async () => {
  update.mockResolvedValue({ id: "kb_3", path: "kb/使用说明.md", title: "使用说明", version: "c9" });
  const user = userEvent.setup();
  open();
  await screen.findByRole("link", { name: /资料说明/ });

  await user.click(screen.getByRole("button", { name: "更多操作" }));
  await user.click(screen.getByRole("menuitem", { name: "重命名" }));
  const input = screen.getByLabelText("资料名称");
  await user.clear(input);
  await user.type(input, "使用说明{Enter}");

  expect(update).toHaveBeenCalledWith("kb/说明.md", "c3", { title: "使用说明" });
  expect(await screen.findByText("已重命名为「使用说明」")).toBeTruthy();
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("移动对话框：进入文件夹、在里面新建文件夹后移到这里", async () => {
  createFolder.mockImplementation(async (path: string) => {
    folders = [...folders, path];
    return { path: `kb/${path}` };
  });
  move.mockResolvedValue({ id: "kb_3", path: "kb/项目/归档/说明.md", previous_path: "kb/说明.md", title: "资料说明", version: "c9" });
  const user = userEvent.setup();
  open();
  await screen.findByRole("link", { name: /资料说明/ });

  await user.click(screen.getByRole("button", { name: "更多操作" }));
  await user.click(screen.getByRole("menuitem", { name: "移动" }));
  const dialog = screen.getByRole("dialog", { name: "移动到…" });
  expect((screen.getByRole("button", { name: "移到这里" }) as HTMLButtonElement).disabled).toBe(true);
  expect(dialog.textContent).toContain("当前位置");

  await user.click(await screen.findByRole("button", { name: "项目" }));
  expect(dialog.textContent).toContain("星云验收纪要");
  await user.click(screen.getByRole("button", { name: "新建文件夹" }));
  await user.type(screen.getByLabelText("新文件夹名称"), "归档");
  await user.click(screen.getByRole("button", { name: "创建" }));
  expect(createFolder).toHaveBeenCalledWith("项目/归档");
  await waitFor(() => expect(dialog.textContent).toContain("这个文件夹是空的"));

  await user.click(screen.getByRole("button", { name: "移到这里" }));
  expect(move).toHaveBeenCalledWith("kb/说明.md", "c3", "项目/归档/说明.md");
  expect(await screen.findByText("已把「资料说明」移动到资料库 / 项目 / 归档")).toBeTruthy();
});

test("删除在对话框里确认；失败原因留在框里", async () => {
  remove.mockRejectedValueOnce(new ApiError("version_conflict", "当前版本为 c9", 409, "c9"));
  const user = userEvent.setup();
  open();
  await screen.findByRole("link", { name: /资料说明/ });

  await user.click(screen.getByRole("button", { name: "更多操作" }));
  await user.click(screen.getByRole("menuitem", { name: "删除" }));
  await user.click(screen.getByRole("button", { name: "删除" }));
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.getByRole("alert").textContent).toContain("被修改过");

  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).toBeNull();
});
