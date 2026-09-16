// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";

const search = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve([]),
    listKbDocuments: () => Promise.resolve({
      directory: "kb",
      documents: [
        { id: "kb_1", path: "kb/项目/星云验收.md", title: "星云验收纪要", tags: ["项目"], version: "c1" },
        { id: "kb_2", path: "kb/inbox/周会.md", title: "周会纪要", tags: null, version: "c2" },
      ],
    }),
    searchKb: (q: string) => search(q),
  };
});

const App = (await import("../App")).default;

afterEach(() => {
  cleanup();
  search.mockReset();
});

test("资料页按目录列出资料，点开进入对应资料", async () => {
  render(<MemoryRouter initialEntries={["/kb"]}><App /></MemoryRouter>);

  const link = await screen.findByRole("link", { name: /星云验收纪要/ });
  expect(link.getAttribute("href")).toBe(`/kb/doc?path=${encodeURIComponent("kb/项目/星云验收.md")}`);
  expect(screen.getByText("项目")).toBeTruthy();
  expect(screen.getByText("2 份资料")).toBeTruthy();
});

test("输入搜索词后显示命中片段，清空后回到目录", async () => {
  search.mockResolvedValue({
    query: "NEBULA",
    results: [{ id: "kb_1", path: "kb/项目/星云验收.md", title: "星云验收纪要", heading: "星云验收纪要 / 结果", snippet: "代号 NEBULA-3390" }],
  });
  const user = userEvent.setup();
  render(<MemoryRouter initialEntries={["/kb"]}><App /></MemoryRouter>);
  await screen.findByRole("link", { name: /周会纪要/ });

  await user.type(screen.getByLabelText("搜索资料"), "NEBULA");

  expect(await screen.findByText("代号 NEBULA-3390")).toBeTruthy();
  expect(search).toHaveBeenCalledWith("NEBULA");
  expect(screen.queryByRole("link", { name: /周会纪要/ })).toBeNull();

  await user.clear(screen.getByLabelText("搜索资料"));
  await waitFor(() => expect(screen.queryByText("代号 NEBULA-3390")).toBeNull());
  expect(await screen.findByRole("link", { name: /周会纪要/ })).toBeTruthy();
});
