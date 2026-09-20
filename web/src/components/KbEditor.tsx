import { defaultValueCtx, Editor, editorViewOptionsCtx, parserCtx, rootCtx, schemaCtx } from "@milkdown/kit/core";
import { clipboard } from "@milkdown/kit/plugin/clipboard";
import { history } from "@milkdown/kit/plugin/history";
import { listener, listenerCtx } from "@milkdown/kit/plugin/listener";
import { commonmark } from "@milkdown/kit/preset/commonmark";
import { gfm } from "@milkdown/kit/preset/gfm";
import { DOMParser, DOMSerializer, type Slice } from "@milkdown/kit/prose/model";
import type { EditorView } from "@milkdown/kit/prose/view";
import { getMarkdown } from "@milkdown/kit/utils";
import { Milkdown, MilkdownProvider, useEditor } from "@milkdown/react";
import { useEffect, useRef, useState, type MutableRefObject } from "react";

import type { KbAsset } from "../api";
import { hasLocalImages, separateTables, splitNoteLines, stripComments } from "../markdown";
import { breakAroundImage, insertImageBlock, replaceMissingImages } from "./imageBlock";
import { applyDescription, assetImageView, markDescribing } from "./imageView";
import { math } from "./mathNodes";

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"];

type Upload = (file: File) => Promise<KbAsset>;
type Describe = (path: string) => Promise<string>;

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
  /** 附加在编辑区容器上的类名，用于调整高度与空内容提示。 */
  className?: string;
  /** 给出时可粘贴或拖入图片：上传后立即插入图片引用。 */
  uploadImage?: Upload;
  /** 插入图片后在后台生成说明，生成好了填进 alt 文本；等待期间图注显示“正在生成说明…”。 */
  describeImage?: Describe;
};


type InnerProps = Omit<Props, "label" | "className"> & {
  onUploading: (delta: number) => void;
  onError: (message: string | null) => void;
};

function imageFiles(data: DataTransfer | null): File[] {
  return Array.from(data?.files ?? []).filter((file) => IMAGE_TYPES.includes(file.type));
}

/**
 * Markdown 文件的所见即所得编辑器（Milkdown，CommonMark + GFM + 公式）：资料正文与长期记忆共用。
 *
 * 资料以 Markdown 文件为准：编辑器读进来的是原文，保存出去的也是 Markdown。
 * 编辑器会按自己的写法重新排版原文（列表符号、转义等），所以“有改动”以序列化后的
 * 基准文本为准：只打开不编辑不会产生保存，也就不会悄悄改写用户的文件。
 *
 * 粘贴的纯文本按 Markdown 解析，贴进来的就是渲染后的样子，而不是一段 Markdown 源码。
 * 紧贴正文的表格先补空行再解析，否则整张表会被并进上一段成为纯文本。
 *
 * 公式写作 `$…$`（行内）与 `$$…$$`（独占一段），显示为排版结果，选中后原地改源码。
 *
 * 原文里的 HTML 由预设按纯文本显示，不进入 DOM 执行；资料可能来自邮件等外部内容。
 *
 * 图片在正文里写作 `assets/…`（相对资料库根目录），显示时换成读取接口的地址；
 * 换地址只发生在节点视图里，复制、序列化与保存的仍是原路径。
 */
function Inner({
  initial, onReady, onChange, onInput, reader, uploadImage, describeImage, onUploading, onError,
}: InnerProps) {
  const callbacks = useRef({ onReady, onChange, onInput, uploadImage, describeImage, onUploading, onError });
  callbacks.current = { onReady, onChange, onInput, uploadImage, describeImage, onUploading, onError };

  // 说明在图片插入之后单独生成，不拖慢插入；生成失败时图注回到“添加图片说明”。
  const describeLater = (view: EditorView, path: string) => {
    const describe = callbacks.current.describeImage;
    if (!describe) return;
    markDescribing(view, path, true);
    void describe(path)
      .then((description) => { applyDescription(view, path, description); })
      .catch(() => undefined)
      .finally(() => { if (!view.isDestroyed) markDescribing(view, path, false); });
  };

  // 图片单独成段；上传期间文档可能已被继续编辑，插入位置超出当前文档时退到末尾。
  const insertImages = (view: EditorView, files: File[], at: number | null) => {
    const upload = callbacks.current.uploadImage;
    if (!upload) return false;
    callbacks.current.onError(null);
    void (async () => {
      for (const file of files) {
        callbacks.current.onUploading(1);
        try {
          const asset = await upload(file);
          const node = view.state.schema.nodes.image.create({ src: asset.path, alt: "" });
          view.dispatch(insertImageBlock(view.state, node, at));
          callbacks.current.onInput();
          describeLater(view, asset.path);
        } catch (error) {
          callbacks.current.onError(`图片上传失败：${error instanceof Error ? error.message : String(error)}`);
        } finally {
          callbacks.current.onUploading(-1);
        }
      }
    })();
    return true;
  };

  const { get, loading } = useEditor((root) =>
    Editor.make()
      .config((ctx) => {
        ctx.set(rootCtx, root);
        ctx.set(defaultValueCtx, initial);
        ctx.get(listenerCtx).markdownUpdated((_ctx, markdown) => {
          callbacks.current.onChange(markdown);
        });
        // 视图自身的 handlePaste 先于剪贴板插件执行：只接管图片文件、需要补空行的纯文本，
        // 以及引用了别的应用本地图片的内容，其余交给插件。
        ctx.update(editorViewOptionsCtx, (prev) => ({
          ...prev,
          handlePaste: (view, event, pasted) => {
            const data = event.clipboardData;
            const images = imageFiles(data);
            if (images.length > 0 && callbacks.current.uploadImage) return insertImages(view, images, null);
            if (!data || data.getData("vscode-editor-data") !== "") return false;
            if (view.state.selection.$from.parent.type.spec.code) return false;
            const schema = ctx.get(schemaCtx);
            const text = data.getData("text/plain");
            const html = data.getData("text/html");
            // 熊掌记等应用复制时图片只剩文件名：纯文本是 Markdown 原文，比它附带的富文本更完整，按 Markdown 解析。
            const local = hasLocalImages(text);
            let slice: Slice;
            if (html === "" || local) {
              const cleaned = stripComments(text);
              const fixed = separateTables(local ? splitNoteLines(cleaned) : cleaned);
              if (!local && fixed === text.replace(/\r\n?/g, "\n")) return false;
              const doc = ctx.get(parserCtx)(fixed);
              if (!doc || typeof doc === "string") return false;
              const dom = DOMSerializer.fromSchema(schema).serializeFragment(doc.content);
              slice = DOMParser.fromSchema(schema).parseSlice(dom);
            } else {
              slice = pasted;
            }
            const { slice: kept, missing } = replaceMissingImages(slice, schema);
            if (slice === pasted && missing.length === 0) return false;
            view.dispatch(view.state.tr.replaceSelection(kept).scrollIntoView());
            return true;
          },
          handleKeyDown: (view, event) => {
            if (event.key !== "Enter" || event.isComposing) return false;
            const tr = breakAroundImage(view.state, event.shiftKey ? "before" : "after");
            if (tr === null) return false;
            view.dispatch(tr);
            return true;
          },
          handleDrop: (view, event) => {
            const images = imageFiles(event.dataTransfer);
            if (images.length === 0 || !callbacks.current.uploadImage) return false;
            const at = view.posAtCoords({ left: event.clientX, top: event.clientY })?.pos ?? null;
            return insertImages(view, images, at);
          },
        }));
      })
      .use(commonmark)
      .use(gfm)
      .use(math)
      .use(assetImageView)
      .use(clipboard)
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

export default function KbEditor({ label, onInput, className, ...props }: Props) {
  const [uploading, setUploading] = useState(0);
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <div className={`md kb-editor${className ? ` ${className}` : ""}`} aria-label={label} onInput={onInput}>
        <MilkdownProvider>
          <Inner {...props} onInput={onInput}
            onUploading={(delta) => setUploading((count) => count + delta)}
            onError={setError} />
        </MilkdownProvider>
      </div>
      {uploading > 0 && <p className="kb-editor-status" role="status">图片上传中…</p>}
      {uploading === 0 && error !== null && <p className="kb-editor-status danger" role="alert">{error}</p>}
    </>
  );
}
