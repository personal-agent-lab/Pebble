// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApiError, type MemorySnapshot } from "../api";

const getMemory = vi.fn();
const saveMemory = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listTasks: () => Promise.resolve([]),
    getMemory: () => getMemory(),
    saveMemory: (...args: unknown[]) => saveMemory(...args),
  };
});

// 真实编辑器依赖浏览器排版能力，jsdom 下换成文本框：载入时报告“序列化后的”正文，
// 模拟编辑器把原文重新排版（列表符号从 * 变成 -），验证只打开不会触发保存。
vi.mock("../components/KbEditor", async () => {
  const { useLayoutEffect } = await import("react");
  return {
    default: ({ initial, onReady, onChange, onInput, reader, label }: {
      initial: string; onReady: (m: string) => void; onChange: (m: string) => void;
      onInput: () => void; reader: { current: (() => string) | null }; label: string;
    }) => {
      const normalized = initial.replace(/^\* /gm, "- ");
      useLayoutEffect(() => {
        const node = document.querySelector<HTMLTextAreaElement>(`textarea[aria-label="${label}"]`);
        reader.current = () => node?.value ?? normalized;
        onReady(normalized);
      }, []); // eslint-disable-line react-hooks/exhaustive-deps
      return (
        <textarea aria-label={label} defaultValue={normalized}
          onInput={(event) => { onInput(); onChange(event.currentTarget.value); }} />
      );
    },
  };
});

const App = (await import("../App")).default;

const USER_DOC = "## 表达\n\n* 回答先给结论\n* 默认使用简体中文";

const snapshot = (): MemorySnapshot => ({
  user: { content: USER_DOC, usage: { chars: USER_DOC.length, limit: 1375 }, version: "u1" },
  memory: { content: "", usage: { chars: 0, limit: 2200 }, version: "m1" },
});

beforeEach(() => { getMemory.mockResolvedValue(snapshot()); });
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const open = async () => {
  render(<MemoryRouter initialEntries={["/memory"]}><App /></MemoryRouter>);
  return screen.findByLabelText("关于你的记忆内容") as Promise<HTMLTextAreaElement>;
};
const section = (title: string) => screen.getByRole("region", { name: title });
const replaceText = (node: HTMLTextAreaElement, value: string) => {
  fireEvent.input(node, { target: { value } });
};

test("两个分区各显示整份内容与容量，只打开不出现保存", async () => {
  const editor = await open();

  expect(editor.value).toBe("## 表达\n\n- 回答先给结论\n- 默认使用简体中文");
  expect(within(section("关于你")).getByText(`${USER_DOC.length} / 1375 字`)).toBeTruthy();
  expect(within(section("事实与约定")).getByText("0 / 2200 字")).toBeTruthy();
  expect(within(section("关于你")).getByText(/你本人/)).toBeTruthy();
  expect(within(section("事实与约定")).getByText(/你以外的事实/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
  expect(screen.getAllByRole("link", { name: /记忆/ })[0].getAttribute("href")).toBe("/memory");
});

test("直接编辑后整份保存，带上读取时的版本", async () => {
  const saved = "## 表达\n\n- 回答先解释推导\n- 默认使用简体中文";
  saveMemory.mockResolvedValue({
    target: "user", changed: true, version: "u2", content: saved,
    usage: { chars: saved.length, limit: 1375 },
  });
  const user = userEvent.setup();
  const editor = await open();

  // 编辑器把空段落写成独占一行的 <br />，保存时去掉。
  replaceText(editor, `<br />\n\n${saved}\n`);
  expect(within(section("关于你")).getByText(`${saved.length} / 1375 字`)).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(saveMemory).toHaveBeenCalledWith("user", saved, "u1");
  expect(await screen.findByText("已保存，从下一轮对话起生效")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
  expect((screen.getByLabelText("关于你的记忆内容") as HTMLTextAreaElement).value).toBe(saved);
});

test("放弃修改恢复原内容", async () => {
  const user = userEvent.setup();
  const editor = await open();

  replaceText(editor, "改了一半");
  await user.click(screen.getByRole("button", { name: "放弃修改" }));

  expect((screen.getByLabelText("关于你的记忆内容") as HTMLTextAreaElement).value)
    .toBe("## 表达\n\n- 回答先给结论\n- 默认使用简体中文");
  expect(screen.queryByRole("button", { name: "保存" })).toBeNull();
  expect(saveMemory).not.toHaveBeenCalled();
});

test("版本冲突时保留输入并提示重新载入", async () => {
  saveMemory.mockRejectedValue(new ApiError("version_conflict", "当前版本为 u9", 409, "u9"));
  const user = userEvent.setup();
  const editor = await open();

  replaceText(editor, "页面上的修改");
  await user.click(screen.getByRole("button", { name: "保存" }));

  expect(await screen.findByText("记忆已被更新")).toBeTruthy();
  expect((screen.getByLabelText("关于你的记忆内容") as HTMLTextAreaElement).value).toBe("页面上的修改");

  getMemory.mockResolvedValue({ ...snapshot(), user: { ...snapshot().user, content: "对话改过的", version: "u9" } });
  await user.click(screen.getByRole("button", { name: "重新载入（放弃本页修改）" }));
  await waitFor(() => expect(screen.queryByText("记忆已被更新")).toBeNull());
  expect((screen.getByLabelText("关于你的记忆内容") as HTMLTextAreaElement).value).toBe("对话改过的");
  expect(getMemory).toHaveBeenCalledTimes(2);
});

test("超出上限时标红并禁止保存变大的内容", async () => {
  getMemory.mockResolvedValue({
    ...snapshot(),
    memory: { content: "甲".repeat(2300), usage: { chars: 2300, limit: 2200 }, version: "m1" },
  });
  const user = userEvent.setup();
  await open();

  const memory = section("事实与约定");
  expect(within(memory).getByText("超出上限 100 字 · 2300 / 2200 字")).toBeTruthy();
  const editor = within(memory).getByLabelText("事实与约定的记忆内容") as HTMLTextAreaElement;

  replaceText(editor, "甲".repeat(2301));
  expect(within(memory).getByText("超出上限，先精简再保存")).toBeTruthy();
  expect((within(memory).getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);

  // 仍然超限但变小的整理可以保存。
  replaceText(editor, "甲".repeat(2250));
  saveMemory.mockRejectedValue(new ApiError("memory_full", "“事实与约定”放不下", 422));
  await user.click(within(memory).getByRole("button", { name: "保存" }));
  expect(saveMemory).toHaveBeenCalledWith("memory", "甲".repeat(2250), "m1");
  expect(await within(memory).findByText(/放不下。先精简或合并已有内容再保存/)).toBeTruthy();
});
