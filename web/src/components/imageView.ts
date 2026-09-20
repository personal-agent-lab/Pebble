import { imageSchema } from "@milkdown/kit/preset/commonmark";
import type { Node } from "@milkdown/kit/prose/model";
import type { EditorView } from "@milkdown/kit/prose/view";
import { $view } from "@milkdown/kit/utils";

import { kbAssetUrl } from "../api";

const DESCRIBING_EVENT = "kb-image-describing";
// 每个编辑器里正在生成说明的图片路径；状态变化时通知图片节点重绘图注。
const describing = new WeakMap<EditorView, Set<string>>();

export function markDescribing(view: EditorView, src: string, active: boolean) {
  const pending = describing.get(view) ?? new Set<string>();
  describing.set(view, pending);
  if (active) pending.add(src);
  else pending.delete(src);
  view.dom.dispatchEvent(new CustomEvent(DESCRIBING_EVENT));
}

/**
 * 把生成好的说明填进仍然没有说明的同一张图片；用户在等待期间已经写了说明、
 * 或者把图片删了，就不覆盖。填说明不进撤销历史，撤销仍然一步撤掉整张图片。
 */
export function applyDescription(view: EditorView, src: string, description: string) {
  if (!description) return false;
  const tr = view.state.tr;
  view.state.doc.descendants((node, pos) => {
    if (node.type.name === "image" && node.attrs.src === src && !node.attrs.alt) {
      tr.setNodeMarkup(pos, undefined, { ...node.attrs, alt: description });
    }
  });
  if (!tr.docChanged) return false;
  view.dispatch(tr.setMeta("addToHistory", false));
  return true;
}

type Attrs = { src: string; alt: string; title: string };

/**
 * 图片节点的显示：图片下方是图注（即 alt 文本），宽度与图片一致，默认显示全文，
 * 特别长时折叠并给出“展开”。点击图注原地修改，排版与显示时相同，只多一层淡底色与操作提示。
 * 图注里的输入不交给编辑器处理，改完一次性写回节点属性。
 */
export const assetImageView = $view(imageSchema.node, () => (initialNode, view, getPos) => {
  let node: Node = initialNode;
  let expanded = false;
  let editing = false;

  const dom = document.createElement("span");
  dom.className = "kb-image";
  dom.contentEditable = "false";
  const img = document.createElement("img");
  const caption = document.createElement("span");
  caption.className = "kb-image-caption";
  caption.tabIndex = 0;
  caption.setAttribute("role", "button");
  caption.title = "点击修改图片说明";
  const more = document.createElement("button");
  more.type = "button";
  more.className = "kb-image-more";
  dom.append(img, caption, more);

  const attrs = () => node.attrs as Attrs;

  // 折叠只在确实超过行数上限时出现；图片宽度变化（加载完成、窗口缩放）后重新判断。
  const measure = () => {
    if (editing) return;
    caption.classList.toggle("kb-image-caption-folded", !expanded);
    const overflows = caption.scrollHeight > caption.clientHeight + 1;
    more.hidden = !(overflows || expanded);
    more.textContent = expanded ? "收起" : "展开";
    if (!overflows && !expanded) caption.classList.remove("kb-image-caption-folded");
  };

  const render = () => {
    const { src, alt, title } = attrs();
    const pending = !alt && (describing.get(view)?.has(src) ?? false);
    img.src = kbAssetUrl(src);
    img.alt = alt;
    if (title) img.title = title;
    else img.removeAttribute("title");
    caption.textContent = alt || (pending ? "正在生成说明…" : "添加图片说明");
    caption.classList.toggle("kb-image-caption-empty", !alt && !pending);
    caption.classList.toggle("kb-image-caption-pending", pending);
    requestAnimationFrame(measure);
  };

  const edit = () => {
    if (!view.editable || editing) return;
    editing = true;
    const input = document.createElement("textarea");
    input.className = "kb-image-caption kb-image-caption-editing";
    // 只在用户确实改了内容时写回：等待期间生成的说明不会被一次空的编辑冲掉。
    const original = attrs().alt;
    input.value = original;
    input.rows = 1;
    input.placeholder = "写一句图片说明";
    input.setAttribute("aria-label", "图片说明");
    const hint = document.createElement("span");
    hint.className = "kb-image-hint";
    hint.textContent = "回车保存 · Esc 取消";
    // 高度随内容增长，和显示时一样只占需要的行数。
    const fit = () => { input.style.height = "auto"; input.style.height = `${input.scrollHeight}px`; };

    const finish = (commit: boolean) => {
      if (!editing) return;
      editing = false;
      const alt = input.value.split(/\s+/).join(" ").trim();
      input.replaceWith(caption);
      hint.replaceWith(more);
      const pos = getPos();
      if (commit && pos !== undefined && alt !== original) {
        view.dispatch(view.state.tr.setNodeMarkup(pos, undefined, { ...node.attrs, alt }));
      } else {
        render();
      }
    };
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); finish(true); }
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
    // 输入过程不算正文改动：只有写回节点属性才会让资料变成“未保存”。
    input.addEventListener("input", (event) => { event.stopPropagation(); fit(); });

    caption.replaceWith(input);
    more.replaceWith(hint);
    fit();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  };

  caption.addEventListener("click", (event) => { event.preventDefault(); edit(); });
  caption.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); edit(); }
  });
  more.addEventListener("click", (event) => {
    event.preventDefault();
    expanded = !expanded;
    measure();
  });
  const onDescribing = () => render();
  view.dom.addEventListener(DESCRIBING_EVENT, onDescribing);
  const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => measure());
  resize?.observe(img);

  render();
  return {
    dom,
    update: (next) => {
      if (next.type !== node.type) return false;
      node = next;
      if (!editing) render();
      return true;
    },
    stopEvent: (event) =>
      event.target instanceof Element
      && event.target.closest(".kb-image-caption, .kb-image-more, .kb-image-hint") !== null,
    ignoreMutation: () => true,
    destroy: () => {
      view.dom.removeEventListener(DESCRIBING_EVENT, onDescribing);
      resize?.disconnect();
    },
  };
});
