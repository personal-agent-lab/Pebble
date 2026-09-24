// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ApiError, DEFAULT_SKILL_SELECTION, type ModelEntry, type SkillSummary } from "../api";
import Composer from "./Composer";

const models: ModelEntry[] = [
  { id: "model-a", label: "Model A", kind: "managed" },
  { id: "custom-id", label: "我的模型", kind: "custom" },
];

const skills: SkillSummary[] = [
  {
    skill_id: "weekly-report", name: "周报整理", description: "按固定分节整理本周周报",
    origin: "user", managed: false, state: "active", revision: "r1", updated_at: "2026-09-22T01:00:00Z",
    last_loaded_at: null,
  },
  {
    skill_id: "meeting-notes", name: "会议纪要", description: "固定格式的会议纪要",
    origin: "review", managed: true, state: "active", revision: "r2", updated_at: "2026-09-22T02:00:00Z",
    last_loaded_at: null,
  },
];

beforeEach(() => {
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:preview"),
    revokeObjectURL: vi.fn(),
  });
  // jsdom 不实现滚动，技能列表的高亮项会调用它。
  Element.prototype.scrollIntoView = () => undefined;
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test("新任务可选模型并支持附件单独发送", async () => {
  const onModelChange = vi.fn();
  const onSubmit = vi.fn(async () => null);
  const { container } = render(<Composer placeholder="随心输入" sending={false}
    model="model-a" models={models} onModelChange={onModelChange} onSubmit={onSubmit} />);

  await userEvent.click(screen.getByRole("button", { name: "选择模型" }));
  await userEvent.click(screen.getByRole("option", { name: /我的模型/ }));
  expect(onModelChange).toHaveBeenCalledWith("custom-id");
  expect(screen.queryByRole("listbox")).toBeNull();

  await userEvent.click(screen.getByRole("button", { name: "选择模型" }));
  await userEvent.keyboard("{ArrowDown}{Enter}");
  expect(onModelChange).toHaveBeenLastCalledWith("custom-id");

  const file = new File(["random"], "notes.md", { type: "text/markdown" });
  await userEvent.upload(container.querySelector("input[type=file]") as HTMLInputElement, file);
  expect(screen.getByText("notes.md")).toBeTruthy();
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(onSubmit).toHaveBeenCalledWith("", [file], DEFAULT_SKILL_SELECTION);
  expect(screen.queryByText("notes.md")).toBeNull();
});

test("图片显示预览、可移除，已有任务模型只读", async () => {
  const onSubmit = vi.fn(async () => null);
  const { container } = render(<Composer placeholder="继续输入" sending={false}
    model="model-a" modelLocked onSubmit={onSubmit} />);
  const image = new File(["image"], "photo.png", { type: "image/png" });
  await userEvent.upload(container.querySelector("input[type=file]") as HTMLInputElement, image);

  expect(screen.queryByRole("button", { name: "选择模型" })).toBeNull();
  expect(screen.getByTitle("该任务的模型已固定").textContent).toBe("model-a");
  expect(container.querySelector("img")?.getAttribute("src")).toBe("blob:preview");
  await userEvent.click(screen.getByRole("button", { name: "移除 photo.png" }));
  expect(screen.queryByText("photo.png")).toBeNull();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:preview");
});

test("上传上限和服务端错误均在 Composer 内明确显示", async () => {
  const serverError = new ApiError("invalid_attachment", "附件未通过校验", 422);
  const onSubmit = vi.fn(async () => serverError);
  const { container } = render(<Composer placeholder="继续输入" sending={false}
    model="model-a" onSubmit={onSubmit} />);
  const input = container.querySelector("input[type=file]") as HTMLInputElement;
  const large = new File(["x"], "large.txt", { type: "text/plain" });
  Object.defineProperty(large, "size", { value: 20 * 1024 * 1024 + 1 });
  fireEvent.change(input, { target: { files: [large] } });
  expect(screen.getByRole("alert").textContent).toContain("超过 20 MB");

  await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "hello");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  expect((await screen.findByRole("alert")).textContent).toContain("附件未通过校验");
});

test("模型目录不是最新时提示并可重试，不阻止发送", async () => {
  const onRetry = vi.fn();
  const onSubmit = vi.fn(async () => null);
  render(<Composer placeholder="随心输入" sending={false} model="model-a" models={models}
    catalogNotice={{ message: "模型目录可能不是最新", retrying: false, onRetry }} onSubmit={onSubmit} />);

  expect(screen.getByText("模型目录可能不是最新")).toBeTruthy();
  await userEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(onRetry).toHaveBeenCalled();
  await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "hello");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(onSubmit).toHaveBeenCalledWith("hello", [], DEFAULT_SKILL_SELECTION);
});

test("输入 / 列出技能，回车选中后挂成胶囊并随消息提交", async () => {
  const onSubmit = vi.fn(async () => null);
  render(<Composer placeholder="随心输入" sending={false} model="model-a"
    skills={skills} onSubmit={onSubmit} />);
  const textbox = screen.getByRole("textbox", { name: "消息" }) as HTMLTextAreaElement;

  await userEvent.type(textbox, "/周");
  expect(screen.getByRole("listbox", { name: "技能列表" })).toBeTruthy();
  expect(screen.queryByRole("option", { name: /会议纪要/ })).toBeNull();
  await userEvent.keyboard("{Enter}");

  expect(textbox.value).toBe("");
  expect(screen.getByLabelText("移除技能 周报整理")).toBeTruthy();
  await userEvent.type(textbox, "按技能整理");
  await userEvent.click(screen.getByRole("button", { name: "发送" }));
  expect(onSubmit).toHaveBeenCalledWith("按技能整理", [], {
    skills: [{ id: "weekly-report" }], excluded_skill_ids: [], auto_match: true,
  });
});

test("方向键移动高亮，Esc 收起列表", async () => {
  const onSubmit = vi.fn(async () => null);
  render(<Composer placeholder="随心输入" sending={false} model="model-a"
    skills={skills} onSubmit={onSubmit} />);
  const textbox = screen.getByRole("textbox", { name: "消息" }) as HTMLTextAreaElement;

  await userEvent.type(textbox, "/");
  expect(screen.getByRole("option", { selected: true }).textContent).toContain("周报整理");
  await userEvent.keyboard("{ArrowDown}");
  expect(screen.getByRole("option", { selected: true }).textContent).toContain("会议纪要");
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("listbox", { name: "技能列表" })).toBeNull();
  expect(textbox.value).toBe("/");
});

test("鼠标悬停把高亮带到悬停项，回车选中的就是它", async () => {
  const onSubmit = vi.fn(async () => null);
  render(<Composer placeholder="随心输入" sending={false} model="model-a"
    skills={skills} onSubmit={onSubmit} />);
  const textbox = screen.getByRole("textbox", { name: "消息" });

  await userEvent.type(textbox, "/");
  await userEvent.hover(screen.getByRole("option", { name: /会议纪要/ }));
  expect(screen.getByRole("option", { selected: true }).textContent).toContain("会议纪要");
  await userEvent.keyboard("{Enter}");
  expect(screen.getByLabelText("移除技能 会议纪要")).toBeTruthy();
});

test("无匹配时回车只收起列表，不当正文发出", async () => {
  const onSubmit = vi.fn(async () => null);
  render(<Composer placeholder="随心输入" sending={false} model="model-a"
    skills={skills} onSubmit={onSubmit} />);
  const textbox = screen.getByRole("textbox", { name: "消息" }) as HTMLTextAreaElement;

  await userEvent.type(textbox, "/zzz");
  expect(screen.getByText("无匹配技能")).toBeTruthy();
  await userEvent.keyboard("{Enter}");
  expect(screen.queryByRole("listbox", { name: "技能列表" })).toBeNull();
  expect(onSubmit).not.toHaveBeenCalled();
  expect(textbox.value).toBe("/zzz");
});

test("点击列表以外收起后，同一段 / 文本继续输入不再弹出；清掉重打才恢复", async () => {
  const onSubmit = vi.fn(async () => null);
  render(<Composer placeholder="随心输入" sending={false} model="model-a"
    skills={skills} onSubmit={onSubmit} />);
  const textbox = screen.getByRole("textbox", { name: "消息" }) as HTMLTextAreaElement;

  await userEvent.type(textbox, "/");
  expect(screen.getByRole("listbox", { name: "技能列表" })).toBeTruthy();

  fireEvent.mouseDown(document.body);
  expect(screen.queryByRole("listbox", { name: "技能列表" })).toBeNull();

  // 粘性收起：继续输入不匹配，再按 Esc 之外的方式也回不来。
  await userEvent.type(textbox, "周");
  expect(screen.queryByRole("listbox", { name: "技能列表" })).toBeNull();

  // 清掉 `/` 重新输入才重新唤起。
  await userEvent.clear(textbox);
  await userEvent.type(textbox, "/");
  expect(screen.getByRole("listbox", { name: "技能列表" })).toBeTruthy();
});

test("点击选项选中；已选技能不再进列表，胶囊可移除", async () => {
  const onSubmit = vi.fn(async () => null);
  render(<Composer placeholder="随心输入" sending={false} model="model-a"
    skills={skills} onSubmit={onSubmit} />);
  const textbox = screen.getByRole("textbox", { name: "消息" }) as HTMLTextAreaElement;

  await userEvent.type(textbox, "/");
  await userEvent.click(screen.getByRole("option", { name: /周报整理/ }));
  expect(textbox.value).toBe("");

  await userEvent.type(textbox, "/");
  expect(screen.queryByRole("option", { name: /周报整理/ })).toBeNull();
  await userEvent.click(screen.getByRole("option", { name: /会议纪要/ }));
  expect(screen.getByLabelText("移除技能 会议纪要")).toBeTruthy();

  await userEvent.click(screen.getByRole("button", { name: "移除技能 周报整理" }));
  expect(screen.queryByLabelText("移除技能 周报整理")).toBeNull();
  expect(screen.getByLabelText("移除技能 会议纪要")).toBeTruthy();
});

test("目录为空时提示去技能页创建；未提供目录时 / 按普通文本输入", async () => {
  const onSubmit = vi.fn(async () => null);
  const { rerender } = render(<Composer placeholder="随心输入" sending={false} model="model-a"
    skills={[]} onSubmit={onSubmit} />);
  await userEvent.type(screen.getByRole("textbox", { name: "消息" }), "/");
  expect(screen.getByText(/还没有启用中的技能/)).toBeTruthy();

  rerender(<Composer placeholder="随心输入" sending={false} model="model-a" onSubmit={onSubmit} />);
  const textbox = screen.getByRole("textbox", { name: "消息" }) as HTMLTextAreaElement;
  await userEvent.type(textbox, "{Backspace}/abc");
  expect(textbox.value).toBe("/abc");
  expect(screen.queryByRole("listbox", { name: "技能列表" })).toBeNull();
});
