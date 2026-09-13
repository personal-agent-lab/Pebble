import ReactMarkdown, { type Components } from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

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
  // 表格可能比阅读栏宽，包一层自己的滚动容器，不让它把消息流撑出横向滚动。
  table: ({ children }) => (
    <div className="md-table">
      <table>{children}</table>
    </div>
  ),
};

const PLUGINS = [remarkGfm, remarkBreaks];

export default function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={PLUGINS} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
