import { defaultValueCtx, Editor, rootCtx } from "@milkdown/kit/core";
import { history } from "@milkdown/kit/plugin/history";
import { listener, listenerCtx } from "@milkdown/kit/plugin/listener";
import { commonmark } from "@milkdown/kit/preset/commonmark";
import { gfm } from "@milkdown/kit/preset/gfm";
import { getMarkdown } from "@milkdown/kit/utils";
import { Milkdown, MilkdownProvider, useEditor } from "@milkdown/react";
import { useEffect, useRef, type MutableRefObject } from "react";

type Props = {
  /** 载入时的 Markdown 原文；换一份资料或换一个版本时由调用方用 key 重建编辑器。 */
  initial: string;
  /** 编辑器把原文解析再序列化一遍之后的文本：判断“有没有改过”要跟它比，不跟原文比。 */
  onReady: (markdown: string) => void;
  onChange: (markdown: string) => void;
  /** 用户一有输入就立刻通知；内容变更通知有防抖，不能靠它决定能不能保存。 */
  onInput: () => void;
  /** 保存时直接读编辑器当前内容，不等防抖后的变更通知。 */
  reader: MutableRefObject<(() => string) | null>;
  label: string;
};

/**
 * 资料正文的所见即所得编辑器（Milkdown，CommonMark + GFM）。
 *
 * 资料以 Markdown 文件为准：编辑器读进来的是原文，保存出去的也是 Markdown。
 * 编辑器会按自己的写法重新排版原文（列表符号、转义等），所以“有改动”以序列化后的
 * 基准文本为准：只打开不编辑不会产生保存，也就不会悄悄改写用户的文件。
 *
 * 原文里的 HTML 由预设按纯文本显示，不进入 DOM 执行；资料可能来自邮件等外部内容。
 */
function Inner({ initial, onReady, onChange, reader }: Omit<Props, "label" | "onInput">) {
  const callbacks = useRef({ onReady, onChange });
  callbacks.current = { onReady, onChange };

  const { get, loading } = useEditor((root) =>
    Editor.make()
      .config((ctx) => {
        ctx.set(rootCtx, root);
        ctx.set(defaultValueCtx, initial);
        ctx.get(listenerCtx).markdownUpdated((_ctx, markdown) => {
          callbacks.current.onChange(markdown);
        });
      })
      .use(commonmark)
      .use(gfm)
      .use(history)
      .use(listener),
  );

  // 基准只在编辑器就绪时报告一次：`get` 会随重渲染换引用，effect 重跑时再报告，
  // 就会把用户已经改过的内容当成基准，保存时误判为“没有改动”。
  const reported = useRef(false);
  useEffect(() => {
    if (loading) return;
    const editor = get();
    if (editor === undefined) return;
    reader.current = () => editor.action(getMarkdown());
    if (!reported.current) {
      reported.current = true;
      callbacks.current.onReady(reader.current());
    }
  }, [loading, get, reader]);
  useEffect(() => () => { reader.current = null; }, [reader]);

  return <Milkdown />;
}

export default function KbEditor({ label, onInput, ...props }: Props) {
  return (
    <div className="md kb-editor" aria-label={label} onInput={onInput}>
      <MilkdownProvider>
        <Inner {...props} />
      </MilkdownProvider>
    </div>
  );
}
