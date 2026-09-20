import { Schema, Slice } from "@milkdown/kit/prose/model";
import { EditorState, NodeSelection, TextSelection } from "@milkdown/kit/prose/state";
import { expect, test } from "vitest";

import { breakAroundImage, insertImageBlock, replaceMissingImages } from "./imageBlock";

const schema = new Schema({
  nodes: {
    doc: { content: "block+" },
    paragraph: { group: "block", content: "inline*" },
    text: { group: "inline" },
    image: { group: "inline", inline: true, atom: true, attrs: { src: { default: "" } } },
  },
});
const p = (...content: ReturnType<typeof schema.text>[]) => schema.nodes.paragraph.create(null, content);
const image = schema.nodes.image.create({ src: "assets/a.png" });
const shape = (state: EditorState) =>
  state.doc.children.map((block) => block.firstChild?.type.name === "image" ? "[图]" : block.textContent);

test("光标在句子中间时，图片插在这一段之后，句子不被劈开", () => {
  const doc = schema.nodes.doc.create(null, [p(schema.text("Event Source: 生产事件的 UI 元素")), p(schema.text("下一段"))]);
  const state = EditorState.create({ doc, selection: TextSelection.create(doc, 13) });
  const next = state.apply(insertImageBlock(state, image, null));
  expect(shape(next)).toEqual(["Event Source: 生产事件的 UI 元素", "[图]", "下一段"]);
  expect(next.selection.$from.parent.textContent).toBe("下一段");
});

test("光标在空段落里时图片占用这一段，图片是最后一段时补一个空段落接着写", () => {
  const doc = schema.nodes.doc.create(null, [p(schema.text("前文")), p()]);
  const state = EditorState.create({ doc, selection: TextSelection.create(doc, 5) });
  const next = state.apply(insertImageBlock(state, image, null));
  expect(shape(next)).toEqual(["前文", "[图]", ""]);
  expect(next.selection.from).toBe(next.doc.content.size - 1);
});

test("后面还有内容时不补空段落，光标落到下一段", () => {
  const list = new Schema({
    nodes: schema.spec.nodes.append({
      list: { group: "block", content: "item+" },
      item: { content: "paragraph+" },
    }),
  });
  const doc = list.nodes.doc.create(null, [
    list.nodes.paragraph.create(null, list.text("标题")),
    list.nodes.list.create(null, list.nodes.item.create(null, list.nodes.paragraph.create(null, list.text("列表项")))),
  ]);
  const state = EditorState.create({ doc, selection: TextSelection.create(doc, 3) });
  const next = state.apply(insertImageBlock(state, list.nodes.image.create({ src: "assets/a.png" }), null));
  expect(next.doc.childCount).toBe(3);
  expect(next.doc.child(2).type.name).toBe("list");
  expect(next.selection.$from.parent.textContent).toBe("列表项");
});

test("拖到块与块之间或超出文档时放在最近的合法位置", () => {
  const doc = schema.nodes.doc.create(null, [p(schema.text("一")), p(schema.text("二"))]);
  const state = EditorState.create({ doc });
  expect(shape(state.apply(insertImageBlock(state, image, 3)))).toEqual(["一", "[图]", "二"]);
  expect(shape(state.apply(insertImageBlock(state, image, 999)))).toEqual(["一", "二", "[图]", ""]);
});

test("光标在“图片未随粘贴带入”的占位段落里时，图片替换这一段", () => {
  const doc = schema.nodes.doc.create(null, [p(schema.text("前文")), p(schema.text("〔图片未随粘贴带入：image.png〕")), p(schema.text("后文"))]);
  const state = EditorState.create({ doc, selection: TextSelection.create(doc, 8) });
  expect(shape(state.apply(insertImageBlock(state, image, null)))).toEqual(["前文", "[图]", "后文"]);
});

test("粘贴内容里显示不出来的图片换成占位文字，能显示的原样保留", () => {
  const remote = schema.nodes.image.create({ src: "https://example.com/a.png" });
  const local = schema.nodes.image.create({ src: "image%202.png" });
  const content = schema.nodes.doc.create(null, [
    schema.nodes.paragraph.create(null, [schema.text("见图"), local]),
    schema.nodes.paragraph.create(null, [remote]),
  ]).content;
  const { slice, missing } = replaceMissingImages(new Slice(content, 0, 0), schema);
  expect(missing).toEqual(["image%202.png"]);
  expect(slice.content.child(0).childCount).toBe(1);
  expect(slice.content.child(0).textContent).toBe("见图〔图片未随粘贴带入：image 2.png〕");
  expect(slice.content.child(1).firstChild?.attrs.src).toBe("https://example.com/a.png");
});

test("选中图片按回车，在图片之后另起一段，光标落进新段落", () => {
  const doc = schema.nodes.doc.create(null, [p(schema.text("前文")), schema.nodes.paragraph.create(null, image), p(schema.text("后文"))]);
  const state = EditorState.create({ doc, selection: NodeSelection.create(doc, 5) });
  const next = state.apply(breakAroundImage(state, "after")!);
  expect(shape(next)).toEqual(["前文", "[图]", "", "后文"]);
  expect(next.selection.$from.parent.content.size).toBe(0);
  expect(next.selection.$from.index(0)).toBe(2);
});

test("图片后面还有文字时从图片之后断开；没选中图片时不接管回车", () => {
  const doc = schema.nodes.doc.create(null, [schema.nodes.paragraph.create(null, [image, schema.text("说明")])]);
  const state = EditorState.create({ doc, selection: NodeSelection.create(doc, 1) });
  const next = state.apply(breakAroundImage(state, "after")!);
  expect(shape(next)).toEqual(["[图]", "说明"]);
  expect(next.selection.$from.parent.textContent).toBe("说明");
  expect(next.selection.$from.parentOffset).toBe(0);
  expect(breakAroundImage(EditorState.create({ doc }), "after")).toBeNull();
});

test("选中图片按 Shift+回车，在图片之前另起一段；图片前面有文字时从图片之前断开", () => {
  const alone = schema.nodes.doc.create(null, [schema.nodes.paragraph.create(null, image), p(schema.text("后文"))]);
  const first = EditorState.create({ doc: alone, selection: NodeSelection.create(alone, 1) });
  const above = first.apply(breakAroundImage(first, "before")!);
  expect(shape(above)).toEqual(["", "[图]", "后文"]);
  expect(above.selection.$from.index(0)).toBe(0);

  const inline = schema.nodes.doc.create(null, [schema.nodes.paragraph.create(null, [schema.text("说明"), image])]);
  const second = EditorState.create({ doc: inline, selection: NodeSelection.create(inline, 3) });
  const split = second.apply(breakAroundImage(second, "before")!);
  expect(shape(split)).toEqual(["说明", "[图]"]);
  expect(split.selection.$from.parent.textContent).toBe("说明");
  expect(split.selection.$from.parentOffset).toBe(2);
});
