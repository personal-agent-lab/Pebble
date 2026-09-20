import { paragraphSchema } from "@milkdown/kit/preset/commonmark";
import { InputRule } from "@milkdown/kit/prose/inputrules";
import type { Node } from "@milkdown/kit/prose/model";
import { NodeSelection, Plugin, Selection, TextSelection } from "@milkdown/kit/prose/state";
import type { EditorView, NodeView } from "@milkdown/kit/prose/view";
import { $inputRule, $nodeSchema, $prose, $remark, $view } from "@milkdown/kit/utils";
import katex from "katex";
import remarkMath from "remark-math";

import { remarkStrictMath } from "../math";

const EDIT_EVENT = "kb-math-edit";

const remarkMathPlugin = $remark("remarkMath", () => remarkMath);
const remarkStrictMathPlugin = $remark("remarkStrictMath", () => remarkStrictMath);

/** 行内公式 `$…$`：整体是一个原子节点，源码存在 value 属性里，序列化时原样写回。 */
export const mathInlineSchema = $nodeSchema("math_inline", () => ({
  group: "inline",
  inline: true,
  atom: true,
  attrs: { value: { default: "" } },
  parseDOM: [{
    tag: "span[data-type=\"math_inline\"]",
    getAttrs: (dom) => ({ value: (dom as HTMLElement).dataset.value ?? "" }),
  }],
  // 复制到别的应用时是一段 `$…$` 文字；编辑器里的显示由节点视图负责。
  toDOM: (node) => ["span", { "data-type": "math_inline", "data-value": node.attrs.value }, `$${node.attrs.value}$`],
  parseMarkdown: {
    match: (node) => node.type === "inlineMath",
    runner: (state, node, type) => { state.addNode(type, { value: node.value as string }); },
  },
  toMarkdown: {
    match: (node) => node.type.name === "math_inline",
    runner: (state, node) => { state.addNode("inlineMath", undefined, node.attrs.value); },
  },
}));

/** 块级公式 `$$…$$`：独占一段，按展示模式排版。 */
export const mathBlockSchema = $nodeSchema("math_block", () => ({
  group: "block",
  atom: true,
  defining: true,
  attrs: { value: { default: "" } },
  parseDOM: [{
    tag: "div[data-type=\"math_block\"]",
    preserveWhitespace: "full",
    getAttrs: (dom) => ({ value: (dom as HTMLElement).dataset.value ?? "" }),
  }],
  toDOM: (node) => ["div", { "data-type": "math_block", "data-value": node.attrs.value }, `$$\n${node.attrs.value}\n$$`],
  parseMarkdown: {
    match: (node) => node.type === "math",
    runner: (state, node, type) => { state.addNode(type, { value: node.value as string }); },
  },
  toMarkdown: {
    match: (node) => node.type.name === "math_block",
    runner: (state, node) => { state.addNode("math", undefined, node.attrs.value); },
  },
}));

/** 输入 `$E=mc^2$` 后立即变成公式；与读入时同一套规则，`$5 到 $` 这类金额不触发。 */
const mathInlineInputRule = $inputRule((ctx) =>
  new InputRule(/(?<![$\\])\$([^\s$](?:[^$]*[^\s$])?)\$$/, (state, match, start, end) =>
    state.tr.replaceWith(start, end, mathInlineSchema.type(ctx).create({ value: match[1] }))),
);

/** 在空段落开头输入 `$$` 加空格，换成一个空的块级公式并直接进入编辑。 */
const mathBlockInputRule = $inputRule((ctx) =>
  new InputRule(/^\$\$\s$/, (state, _match, start, end) => {
    const $start = state.doc.resolve(start);
    const rest = $start.parent.content.size - state.doc.resolve(end).parentOffset;
    if ($start.parent.type !== paragraphSchema.type(ctx) || rest > 0) return null;
    const before = $start.before();
    const tr = state.tr.replaceWith(before, $start.after(), mathBlockSchema.type(ctx).create());
    return tr.setSelection(NodeSelection.create(tr.doc, before));
  }),
);

function render(target: HTMLElement, value: string, displayMode: boolean) {
  if (value.trim() === "") {
    target.textContent = displayMode ? "空公式" : "公式";
    target.classList.add("kb-math-empty");
    return;
  }
  target.classList.remove("kb-math-empty");
  katex.render(value, target, { displayMode, throwOnError: false });
}

/**
 * 公式节点的显示与编辑：平时显示 KaTeX 排版结果。点击公式，或选中后按回车，原地换成
 * 源码输入框；方向键经过、粘贴或撤销落在公式上都不会进入编辑。
 * 回车（块级为 Mod+回车）或失焦写回，Esc 放弃；在源码开头或末尾继续按方向键会离开公式。
 * 输入过程不算正文改动，写回后才触发变更；写回为空则删掉这个公式。
 * 用 `$$` 新建的空公式直接进入编辑。
 */
function mathView(displayMode: boolean) {
  return (initialNode: Node, view: EditorView, getPos: () => number | undefined): NodeView => {
    let node = initialNode;
    let editor: HTMLInputElement | HTMLTextAreaElement | null = null;
    let hint: HTMLElement | null = null;

    const dom = document.createElement(displayMode ? "div" : "span");
    dom.className = displayMode ? "kb-math kb-math-block" : "kb-math";
    dom.contentEditable = "false";
    const output = document.createElement(displayMode ? "div" : "span");
    output.className = "kb-math-output";
    dom.append(output);
    const show = () => render(output, node.attrs.value as string, displayMode);

    const finish = (commit: boolean, exit: "before" | "after" | null) => {
      const input = editor;
      if (input === null) return;
      editor = null;
      input.remove();
      hint?.remove();
      hint = null;
      output.hidden = false;
      dom.classList.remove("kb-math-editing");
      const pos = getPos();
      if (pos === undefined) return;
      const value = input.value.trim();
      const removed = commit && value === "";
      const tr = view.state.tr;
      if (removed) tr.delete(pos, pos + node.nodeSize);
      else if (commit && value !== node.attrs.value) tr.setNodeMarkup(pos, undefined, { value });
      else show();
      if (exit !== null || removed) {
        let at = exit === "before" || removed ? pos : pos + node.nodeSize;
        // 块级公式在文档首尾时两侧没有可停光标的段落，补一个空段落，否则光标又会落回公式本身。
        if (displayMode && !removed) {
          const paragraph = view.state.schema.nodes.paragraph.create();
          if (exit === "after" && tr.doc.resolve(at).nodeAfter === null) tr.insert(at, paragraph);
          if (exit === "before" && tr.doc.resolve(at).nodeBefore === null) { tr.insert(at, paragraph); at += 1; }
        }
        const $at = tr.doc.resolve(Math.min(at, tr.doc.content.size));
        tr.setSelection(displayMode ? Selection.near($at, exit === "before" ? -1 : 1) : TextSelection.create(tr.doc, $at.pos));
      }
      if (tr.docChanged || tr.selectionSet) view.dispatch(tr);
      if (exit !== null || removed) view.focus();
    };

    const edit = () => {
      if (!view.editable || editor !== null) return;
      const input: HTMLInputElement | HTMLTextAreaElement = displayMode ? document.createElement("textarea") : document.createElement("input");
      input.className = "kb-math-source";
      input.value = node.attrs.value as string;
      input.spellcheck = false;
      input.setAttribute("aria-label", displayMode ? "块级公式源码" : "公式源码");
      input.placeholder = "LaTeX";
      if (input instanceof HTMLTextAreaElement) input.rows = 1;
      const fit = () => {
        if (input instanceof HTMLTextAreaElement) { input.style.height = "auto"; input.style.height = `${input.scrollHeight}px`; }
        else input.size = Math.max(4, input.value.length + 1);
      };
      (input as HTMLElement).addEventListener("keydown", (event) => {
        if (event.isComposing) return;
        const atStart = input.selectionStart === 0 && input.selectionEnd === 0;
        const atEnd = input.selectionStart === input.value.length && input.selectionEnd === input.value.length;
        const submit = event.key === "Enter" && (!displayMode || event.metaKey || event.ctrlKey);
        let exit: "before" | "after" | null | undefined;
        if (submit) exit = "after";
        else if (event.key === "Escape") { event.preventDefault(); finish(false, "after"); return; }
        else if ((event.key === "ArrowLeft" || (displayMode && event.key === "ArrowUp")) && atStart) exit = "before";
        else if ((event.key === "ArrowRight" || (displayMode && event.key === "ArrowDown")) && atEnd) exit = "after";
        if (exit === undefined) return;
        event.preventDefault();
        finish(true, exit);
      });
      input.addEventListener("blur", () => finish(true, null));
      input.addEventListener("input", (event) => {
        event.stopPropagation();
        fit();
        if (displayMode) render(output, input.value, true);
      });
      editor = input;
      dom.classList.add("kb-math-editing");
      // 块级公式编辑时源码在上、排版预览在下，随输入更新；行内公式只显示源码，不撑乱所在的行。
      if (displayMode) {
        hint = document.createElement("span");
        hint.className = "kb-math-hint";
        hint.textContent = "⌘/Ctrl + 回车保存 · Esc 取消";
        dom.prepend(input, hint);
      } else {
        output.hidden = true;
        dom.append(input);
      }
      fit();
      input.focus();
    };

    dom.addEventListener("click", (event) => {
      if (editor !== null) return;
      event.preventDefault();
      const pos = getPos();
      if (pos !== undefined) view.dispatch(view.state.tr.setSelection(NodeSelection.create(view.state.doc, pos)));
      edit();
    });
    dom.addEventListener(EDIT_EVENT, edit);

    show();
    return {
      dom,
      update: (next) => {
        if (next.type !== node.type) return false;
        node = next;
        if (editor === null) show();
        return true;
      },
      selectNode: () => {
        dom.classList.add("ProseMirror-selectednode");
        if (node.attrs.value === "") edit();
      },
      deselectNode: () => { dom.classList.remove("ProseMirror-selectednode"); },
      stopEvent: (event) => event.target instanceof Element && event.target.closest(".kb-math-source, .kb-math-hint") !== null,
      ignoreMutation: () => true,
    };
  };
}

/** 选中公式时按回车进入编辑。 */
const mathEditKey = $prose(() => new Plugin({
  props: {
    handleKeyDown: (view, event) => {
      const { selection } = view.state;
      if (event.key !== "Enter" || event.isComposing || !(selection instanceof NodeSelection)) return false;
      if (!selection.node.type.name.startsWith("math_")) return false;
      view.nodeDOM(selection.from)?.dispatchEvent(new CustomEvent(EDIT_EVENT));
      return true;
    },
  },
}));

const mathInlineView = $view(mathInlineSchema.node, () => mathView(false));
const mathBlockView = $view(mathBlockSchema.node, () => mathView(true));

/** 公式支持：`$…$` 行内公式与 `$$…$$` 块级公式，读入、显示、编辑并按原写法保存。 */
export const math = [
  remarkMathPlugin,
  remarkStrictMathPlugin,
  mathInlineSchema,
  mathBlockSchema,
  mathInlineInputRule,
  mathBlockInputRule,
  mathInlineView,
  mathBlockView,
  mathEditKey,
].flat();
