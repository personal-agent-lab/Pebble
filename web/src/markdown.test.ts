import { expect, test } from "vitest";

import {
  hasLocalImages,
  isMissingImagePlaceholder,
  isResolvableImage,
  missingImagePlaceholder,
  separateTables,
  splitNoteLines,
  stripComments,
} from "./markdown";

test("紧贴在列表项文字下一行的表格前补空行", () => {
  const text = "2. **各师编制** 三个师：\n| 师番号 | 兵力 |\n|---|---|\n| 第115师 | 15000 |\n- 下一项";
  expect(separateTables(text)).toBe(
    "2. **各师编制** 三个师：\n\n| 师番号 | 兵力 |\n|---|---|\n| 第115师 | 15000 |\n- 下一项",
  );
});

test("紧跟在表格最后一行下面的正文前补空行，不并入表格", () => {
  const text = "| a | b |\n| --- | --- |\n| 1 | 2 |\n与下表通用的如下：\n| c | d |\n| --- | --- |\n| 3 | 4 |";
  expect(separateTables(text)).toBe(
    "| a | b |\n| --- | --- |\n| 1 | 2 |\n\n与下表通用的如下：\n\n| c | d |\n| --- | --- |\n| 3 | 4 |",
  );
  const listed = "| a | b |\n|---|---|\n| 1 | 2 |\n- 下一项";
  expect(separateTables(listed)).toBe(listed);
});

test("已有空行、不是表格或在代码块里时原样保留", () => {
  const spaced = "说明：\n\n| a | b |\n| :-- | --: |\n| 1 | 2 |";
  expect(separateTables(spaced)).toBe(spaced);
  const plain = "a | b\n----\nc";
  expect(separateTables(plain)).toBe(plain);
  const fenced = "```\n说明：\n| a | b |\n|---|---|\n```";
  expect(separateTables(fenced)).toBe(fenced);
});

test("熊掌记复制出来的本地图片文件名识别为带不进来的图片，网络图片与资料库图片不算", () => {
  expect(hasLocalImages('## 标题\n![](image.png)<!-- {"width":219} -->\n- 列表')).toBe(true);
  expect(hasLocalImages("![图](https://example.com/a.png) ![说明](assets/3f2a.png)")).toBe(false);
  expect(isResolvableImage("file:///image.png")).toBe(false);
  expect(isResolvableImage("assets/a/b.png")).toBe(false);
});

test("去掉 HTML 注释；占位文字只留文件名并解码", () => {
  expect(stripComments('![](image.png)<!-- {"width":219} -->\n正文')).toBe("![](image.png)\n正文");
  const placeholder = missingImagePlaceholder("image%202.png");
  expect(placeholder).toBe("〔图片未随粘贴带入：image 2.png〕");
  expect(missingImagePlaceholder("file:///Users/x/image.png")).toBe("〔图片未随粘贴带入：image.png〕");
  expect(isMissingImagePlaceholder(` ${placeholder} `)).toBe(true);
  expect(isMissingImagePlaceholder(`说明 ${placeholder}`)).toBe(false);
});

test("笔记应用一行一段：图片行与相邻的普通文字行各自成段，列表、表格与代码块不动", () => {
  const note = [
    "### Activity Back Stack",
    "![](image%202.png)",
    "Activity 是核心组件。",
    "新 Activity 创建时入栈。",
    "- 列表一",
    "- 列表二",
    "| a | b |",
    "|---|---|",
    "```",
    "第一行",
    "第二行",
    "```",
  ].join("\n");
  expect(splitNoteLines(note)).toBe([
    "### Activity Back Stack",
    "",
    "![](image%202.png)",
    "",
    "Activity 是核心组件。",
    "",
    "新 Activity 创建时入栈。",
    "- 列表一",
    "- 列表二",
    "| a | b |",
    "|---|---|",
    "```",
    "第一行",
    "第二行",
    "```",
  ].join("\n"));
});

test("块级公式的 $$ 围栏里不补空行，公式与前后文字各自成段", () => {
  const text = "目标节点取算术平均值。\n$$\n(x,y)=\\frac{1}{N}\\sum x_i\n$$\n这种方式成本极低。";
  expect(splitNoteLines(text)).toBe(text);
  const inline = "第一行 $$x$$\n第二行";
  expect(splitNoteLines(inline)).toBe("第一行 $$x$$\n\n第二行");
});
