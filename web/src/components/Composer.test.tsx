// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { useState } from "react";

import { ApiError, type ModelEntry } from "../api";
import Composer from "./Composer";
import { emptySelection } from "../features/skills/api";

const models: ModelEntry[] = [
  { id: "model-a", label: "Model A", kind: "managed" },
  { id: "custom-id", label: "我的模型", kind: "custom" },
];

beforeEach(() => {
  vi.stubGlobal("URL", {
    createObjectURL: vi.fn(() => "blob:preview"),
    revokeObjectURL: vi.fn(),
  });
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
  expect(onSubmit).toHaveBeenCalledWith("", [file]);
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
  expect(onSubmit).toHaveBeenCalledWith("hello", []);
});

test("加号菜单可选 Skill 或文件，Skill 选择保留在输入框内", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify([
    { id: "sk_one", name: "整理", description: "整理步骤", status: "approved", content_hash: "sha256:one" },
  ]), { headers: { "Content-Type": "application/json" } }));
  const onSubmit = vi.fn(async () => null);
  function Harness() {
    const [selection, setSelection] = useState(emptySelection);
    return <Composer placeholder="随心输入" sending={false} model="model-a" onSubmit={onSubmit}
      selection={selection} onSelectionChange={setSelection} />;
  }
  const { container } = render(<MemoryRouter><Harness /></MemoryRouter>);
  expect(screen.queryByText("添加 Skill")).toBeNull();
  await userEvent.click(screen.getByRole("button", { name: "添加内容" }));
  expect(screen.getAllByRole("menuitem")).toHaveLength(2);
  await userEvent.click(screen.getByRole("menuitem", { name: "Skill" }));
  expect(await screen.findByRole("checkbox", { name: /整理/ })).toBeTruthy();
  await userEvent.click(screen.getByRole("checkbox", { name: /整理/ }));
  expect(container.querySelector(".codex-composer .skill-picker")?.textContent).toContain("整理 ×");
  await userEvent.click(screen.getByRole("button", { name: "关闭 Skill 选择器" }));
  await userEvent.click(screen.getByRole("button", { name: "添加内容" }));
  const fileInput = container.querySelector("input[type=file]") as HTMLInputElement;
  const click = vi.spyOn(fileInput, "click");
  await userEvent.click(screen.getByRole("menuitem", { name: "文件" }));
  expect(click).toHaveBeenCalledOnce();
});
