// @vitest-environment jsdom
import { defaultValueCtx, Editor, editorViewCtx, rootCtx } from "@milkdown/kit/core";
import { commonmark } from "@milkdown/kit/preset/commonmark";
import { gfm } from "@milkdown/kit/preset/gfm";
import { NodeSelection } from "@milkdown/kit/prose/state";
import { getMarkdown } from "@milkdown/kit/utils";
import { afterEach, expect, test } from "vitest";

import { math } from "./mathNodes";

const editors: Editor[] = [];
afterEach(async () => { await Promise.all(editors.splice(0).map((editor) => editor.destroy())); });

async function open(markdown: string) {
  const root = document.createElement("div");
  document.body.append(root);
  const editor = await Editor.make()
    .config((ctx) => { ctx.set(rootCtx, root); ctx.set(defaultValueCtx, markdown); })
    .use(commonmark).use(gfm).use(math)
    .create();
  editors.push(editor);
  return { editor, root, view: editor.action((ctx) => ctx.get(editorViewCtx)) };
}

const nodes = (editor: Editor, name: string) => {
  const found: string[] = [];
  editor.action((ctx) => ctx.get(editorViewCtx)).state.doc.descendants((node) => {
    if (node.type.name === name) found.push(node.attrs.value as string);
  });
  return found;
};

test("行内与块级公式读成公式节点，只打开不编辑时源码原样写回，不被转义", async () => {
  const text = [
    "接收时间是 $\\bar{t_r}$，距离近似为 $\\rho_i = (\\bar{t_r}-t_i)c$。",
    "",
    "$$",
    "\\hat{\\delta_t} = \\bar{t_r} - t_i",
    "$$",
    "",
  ].join("\n");
  const { editor, root } = await open(text);
  expect(nodes(editor, "math_inline")).toEqual(["\\bar{t_r}", "\\rho_i = (\\bar{t_r}-t_i)c"]);
  expect(nodes(editor, "math_block")).toEqual(["\\hat{\\delta_t} = \\bar{t_r} - t_i"]);
  expect(editor.action(getMarkdown())).toBe(text);
  expect(root.querySelectorAll(".kb-math .katex")).toHaveLength(3);
});

test("金额里的 $ 不当公式，保持文字", async () => {
  const { editor } = await open("价格从 $5 涨到 $10，套餐是 $3/月到$8/月。\n");
  expect(nodes(editor, "math_inline")).toEqual([]);
  expect(editor.action((ctx) => ctx.get(editorViewCtx)).state.doc.textContent).toBe("价格从 $5 涨到 $10，套餐是 $3/月到$8/月。");
});

test("点击公式换成源码输入框，回车写回并把光标放到公式后面", async () => {
  const { editor, root, view } = await open("前 $a+b$ 后\n");
  let pos = -1;
  view.state.doc.descendants((node, at) => { if (node.type.name === "math_inline") pos = at; });
  root.querySelector<HTMLElement>(".kb-math")!.click();
  const input = root.querySelector<HTMLInputElement>("input.kb-math-source");
  expect(input?.value).toBe("a+b");
  input!.value = "a^2+b^2";
  input!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  expect(root.querySelector(".kb-math-source")).toBeNull();
  expect(editor.action(getMarkdown())).toBe("前 $a^2+b^2$ 后\n");
  expect(view.state.selection.from).toBe(pos + 1);
});

test("方向键选中公式不进入编辑，按回车才进入；源码改成空的写回时删掉这个公式", async () => {
  const { editor, root, view } = await open("前 $x$ 后\n");
  let pos = -1;
  view.state.doc.descendants((node, at) => { if (node.type.name === "math_inline") pos = at; });
  view.dispatch(view.state.tr.setSelection(NodeSelection.create(view.state.doc, pos)));
  expect(root.querySelector(".kb-math-source")).toBeNull();
  view.someProp("handleKeyDown", (handle) => handle(view, new KeyboardEvent("keydown", { key: "Enter" })));
  const input = root.querySelector<HTMLInputElement>("input.kb-math-source")!;
  input.value = " ";
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  expect(nodes(editor, "math_inline")).toEqual([]);
});

test("块级公式紧跟在段落文字下一行时仍读成块级公式", async () => {
  const { editor } = await open("取算术平均值。\n$$\nx = \\frac{1}{N}\n$$\n这种方式成本极低。\n");
  expect(nodes(editor, "math_block")).toEqual(["x = \\frac{1}{N}"]);
});
