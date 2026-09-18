import { Fragment, Slice, type Node, type Schema } from "@milkdown/kit/prose/model";
import { NodeSelection, Selection, TextSelection, type EditorState, type Transaction } from "@milkdown/kit/prose/state";
import { insertPoint } from "@milkdown/kit/prose/transform";

import { isMissingImagePlaceholder, isResolvableImage, missingImagePlaceholder } from "../markdown";

/**
 * 把图片作为独立的一段插入：光标在空段落或只有“图片未随粘贴带入”占位的段落里就占用这一段，
 * 在有文字的段落里就插到这一段之后，
 * 不把句子劈成两半。`at` 为 null 时以当前光标为准；超出当前文档时退到末尾。
 * 插入后光标落到图片之后；图片是最后一段时补一个空段落，可以接着往下写，否则不另加空行。
 */
export function insertImageBlock(state: EditorState, image: Node, at: number | null): Transaction {
  const { tr, pos } = placeImageBlock(state, image, at);
  const after = pos + tr.doc.nodeAt(pos)!.nodeSize;
  if (tr.doc.resolve(after).nodeAfter === null) {
    tr.insert(after, state.schema.nodes.paragraph.create());
    return tr.setSelection(TextSelection.create(tr.doc, after + 1)).scrollIntoView();
  }
  return tr.setSelection(Selection.near(tr.doc.resolve(after))).scrollIntoView();
}

/**
 * 选中图片时按回车在图片之后另起一段，按 Shift+回车在图片之前另起一段，光标放进新段落。
 * 图片段落没有可见的光标位置，紧挨着表格等块时，这是在两者之间写字的入口；
 * 图片同一段里前后还有文字时，从图片处断开，光标落到断开的那段文字上。
 */
export function breakAroundImage(state: EditorState, side: "before" | "after"): Transaction | null {
  const selection = state.selection;
  if (!(selection instanceof NodeSelection) || selection.node.type.name !== "image") return null;
  const $pos = side === "after" ? selection.$to : selection.$from;
  if (!$pos.parent.isTextblock) return null;
  const tr = state.tr;
  if (side === "after") {
    if ($pos.pos === $pos.end()) {
      tr.insert($pos.after(), state.schema.nodes.paragraph.create());
      return tr.setSelection(TextSelection.create(tr.doc, $pos.after() + 1)).scrollIntoView();
    }
    tr.split($pos.pos);
    return tr.setSelection(TextSelection.create(tr.doc, $pos.pos + 2)).scrollIntoView();
  }
  if ($pos.pos === $pos.start()) {
    tr.insert($pos.before(), state.schema.nodes.paragraph.create());
    return tr.setSelection(TextSelection.create(tr.doc, $pos.before() + 1)).scrollIntoView();
  }
  tr.split($pos.pos);
  return tr.setSelection(TextSelection.create(tr.doc, $pos.pos)).scrollIntoView();
}

/** 按规则放入图片段落，返回事务与该段落的起始位置。 */
function placeImageBlock(state: EditorState, image: Node, at: number | null): { tr: Transaction; pos: number } {
  const paragraph = state.schema.nodes.paragraph.create(null, image);
  const size = state.doc.content.size;
  const $pos = state.doc.resolve(at === null ? state.selection.from : Math.min(at, size));
  const parent = $pos.parent;
  if (!parent.isTextblock || $pos.depth === 0) {
    // 落在块与块之间（例如拖到两段之间）：找最近能放段落的位置。
    const point = insertPoint(state.doc, $pos.pos, paragraph.type) ?? size;
    return { tr: state.tr.insert(point, paragraph), pos: point };
  }
  if (parent.type === state.schema.nodes.paragraph
    && (parent.content.size === 0 || isMissingImagePlaceholder(parent.textContent))) {
    return { tr: state.tr.replaceWith($pos.before(), $pos.after(), paragraph), pos: $pos.before() };
  }
  return { tr: state.tr.insert($pos.after(), paragraph), pos: $pos.after() };
}

/**
 * 把粘贴内容里显示不出来的图片（别的应用本地的文件名）换成占位文字，返回换过的内容与缺失的图片。
 * 能显示的网络图片与资料库图片原样保留。
 */
export function replaceMissingImages(slice: Slice, schema: Schema): { slice: Slice; missing: string[] } {
  const missing: string[] = [];
  const map = (fragment: Fragment): Fragment => {
    let result = Fragment.empty;
    fragment.forEach((node) => {
      let next: Node = node;
      if (node.type === schema.nodes.image && !isResolvableImage(String(node.attrs.src))) {
        missing.push(String(node.attrs.src));
        next = schema.text(missingImagePlaceholder(String(node.attrs.src)));
      } else if (!node.isLeaf) {
        next = node.copy(map(node.content));
      }
      // append 会合并相邻的同样式文字，占位文字不会和前后文字断成几段。
      result = result.append(Fragment.from(next));
    });
    return result;
  };
  return { slice: new Slice(map(slice.content), slice.openStart, slice.openEnd), missing };
}
