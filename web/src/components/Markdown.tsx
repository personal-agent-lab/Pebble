import ReactMarkdown, { type Components } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

import { kbAssetUrl } from "../api";
import { remarkStrictMath } from "../math";

/**
 * Agent 消息的 Markdown 渲染。
 *
 * 不接 rehype-raw：源文本里的 HTML 一律当字面量，不进 DOM。消息内容最终来自
 * 邮件与网页等外部来源，渲染器是这条链路上唯一的展示出口，不能让它执行标记。
 * 链接同理——react-markdown 默认已经挡掉 javascript: 一类协议，这里只补 target 与 rel。
 *
 * 接 remark-breaks：CommonMark 把单个换行当软换行、合并成一段，但 Agent 写的邮件
 * 草稿、地址、清单里行结构本身就是内容（`> **收件人：**` 与 `> **主题：**` 是两行，
 * 不是一句话）。这里按聊天界面的惯例让单换行仍然换行，也与用户消息的 pre-wrap 一致。
 *
 * 接 remark-math 与 rehype-katex：`$…$` 与 `$$…$$` 排成公式。KaTeX 只输出排版结果，
 * 默认不信任 `\href` 一类命令，不会把源文本变成可执行的标记。金额里的 `$` 按
 * remarkStrictMath 的规则保持字面量。
 *
 * 流式过程中文本是半截的（未闭合的 `**`、只写了一半的表格），按 Markdown 解析会
 * 先显示成字面量再随后续 token 归位。这是预期行为，不做"等写完再渲染"的缓冲：
 * 缓冲会让整段回复在生成结束前一直空着。
 */
const COMPONENTS: Components = {
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  // 资料正文里的图片写作 `assets/…`，换成读取接口的地址才能显示。
  // 图片限高显示，点开在新标签页看原图。
  img: ({ src, alt, title }) => {
    const url = typeof src === "string" ? kbAssetUrl(src) : undefined;
    return (
      <a href={url} target="_blank" rel="noopener noreferrer">
        <img src={url} alt={alt} title={title} />
      </a>
    );
  },
  // 表格可能比阅读栏宽，包一层自己的滚动容器，不让它把消息流撑出横向滚动。
  table: ({ children }) => (
    <div className="md-table">
      <table>{children}</table>
    </div>
  ),
};

const PLUGINS = [remarkGfm, remarkMath, remarkStrictMath, remarkBreaks];
const REHYPE_PLUGINS = [rehypeKatex];

export default function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={PLUGINS} rehypePlugins={REHYPE_PLUGINS} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
