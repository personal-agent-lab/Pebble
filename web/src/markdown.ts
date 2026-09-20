// 代码围栏与块级公式的 `$$` 围栏：围栏里的内容原样保留。同一行里开合的 `$$…$$` 不是围栏。
const FENCE = /^\s*(```|~~~|\$\$(?!.*\$\$))/;
const TABLE_ROW = /^\s*\|/;
const DELIMITER_ROW = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

/**
 * 在紧贴正文的表格前后各补一个空行。
 *
 * 表格紧跟在列表项的文字下一行时，按 CommonMark 会被当作该段的续行，整张表变成一段纯文本；
 * AI 对话里复制出来的 Markdown 常是这样。表格后紧跟的一行正文，按 GFM 会被当成表格的一行
 * （熊掌记复制出来的表格与后文之间就没有空行）。补空行后表格独立成块。
 * 代码块里的内容原样保留。
 */
export function separateTables(markdown: string): string {
  const lines = markdown.split(/\r\n?|\n/);
  const out: string[] = [];
  let fenced = false;
  // 所在表格的表头是否以 `|` 开头；不在表格里时为 null。
  let table: boolean | null = null;
  lines.forEach((line, index) => {
    if (FENCE.test(line)) {
      fenced = !fenced;
      table = null;
    }
    const previous = out[out.length - 1];
    const next = lines[index + 1];
    if (table !== null && !fenced) {
      if (line.trim() === "") table = null;
      else if (!line.includes("|") || (table && !TABLE_ROW.test(line))) {
        table = null;
        if (!startsBlock(line)) out.push("");
      }
    }
    if (
      !fenced
      && table === null
      && line.includes("|")
      && next !== undefined && next.includes("|") && next.includes("-") && DELIMITER_ROW.test(next)
    ) {
      if (previous !== undefined && previous.trim() !== "" && !TABLE_ROW.test(previous)) out.push("");
      table = TABLE_ROW.test(line);
    }
    out.push(line);
  });
  return out.join("\n");
}

/** 自己就能结束表格的块起始行：标题、引用、列表、分隔线、代码围栏。 */
function startsBlock(line: string): boolean {
  return /^\s{0,3}(#{1,6}\s|>|[-*+]\s|\d+[.)]\s|(\*\s*){3,}$|(-\s*){3,}$|(_\s*){3,}$|```|~~~)/.test(line);
}

const MARKDOWN_IMAGE = /!\[[^\]]*\]\(\s*<?([^)\s>]+)/g;
const MISSING_IMAGE = /^〔图片未随粘贴带入：[^〕]*〕$/;

/** 能显示的图片地址：网络图片、内嵌数据与资料库自己的 `assets/…`；其余是别的应用本地的文件名。 */
export function isResolvableImage(src: string): boolean {
  return /^(https?:|data:image\/|assets\/[^/]+$)/i.test(src.trim());
}

/** 粘贴的 Markdown 里有没有引用别的应用本地图片（例如熊掌记复制出来的 `![](image.png)`）。 */
export function hasLocalImages(markdown: string): boolean {
  return Array.from(markdown.matchAll(MARKDOWN_IMAGE)).some((match) => !isResolvableImage(match[1]));
}

/** 去掉 HTML 注释：熊掌记在图片后写 `<!-- {"width":219} -->`，编辑器会把它当原文显示出来。 */
export function stripComments(markdown: string): string {
  return markdown.replace(/<!--[\s\S]*?-->/g, "");
}

/** 带不进来的图片在原位留下的占位文字；光标停在只有占位的段落里再粘贴图片时会替换这一段。 */
export function missingImagePlaceholder(src: string): string {
  const name = src.trim().replace(/^file:\/\//i, "").split(/[/\\]/).pop() ?? src;
  let decoded = name;
  try { decoded = decodeURIComponent(name); } catch { /* 保留原文 */ }
  return `〔图片未随粘贴带入：${decoded}〕`;
}

export function isMissingImagePlaceholder(text: string): boolean {
  return MISSING_IMAGE.test(text.trim());
}

const IMAGE_LINE = /^\s*!\[[^\]]*\]\([^)]*\)\s*$/;
// 列表、表格、引用、标题与缩进续行自带结构，不在它们之间插空行。
const STRUCTURED_LINE = /^(\s|[-*+]\s|\d+[.)]\s|\||>|#)/;

/**
 * 笔记应用（如熊掌记）的 Markdown 一行就是一段：相邻两行在 CommonMark 里会并成一段，
 * 图片行也会和下一段文字挤在一起。这里给图片行前后、以及相邻的普通文字行之间补空行；
 * 列表、表格、引用、标题、缩进续行与代码块保持原样。
 */
export function splitNoteLines(markdown: string): string {
  const lines = markdown.split(/\r\n?|\n/);
  const out: string[] = [];
  let fenced = false;
  let previous = "";
  for (const line of lines) {
    const fence = FENCE.test(line);
    if (!fenced && !fence && line.trim() !== "" && previous.trim() !== "") {
      const image = IMAGE_LINE.test(line) || IMAGE_LINE.test(previous);
      const plain = !STRUCTURED_LINE.test(line) && !STRUCTURED_LINE.test(previous);
      if (image || plain) out.push("");
    }
    if (fence) fenced = !fenced;
    out.push(line);
    previous = fence ? "" : line;
  }
  return out.join("\n");
}
