import { ArrowUp, FileText, Plus, Sparkle, X } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";

import type { ApiError, ModelEntry } from "../api";
import ModelPicker from "./ModelPicker";
import SkillPicker from "../features/skills/SkillPicker";
import type { Selection } from "../features/skills/api";

const MAX_FILES = 10;
const MAX_FILE_SIZE = 20 * 1024 * 1024;
const ACCEPT = ".png,.jpg,.jpeg,.webp,.pdf,.txt,.md,.markdown,.py,.js,.jsx,.ts,.tsx,.json,.yaml,.yml,.xml,.csv,.html,.css,.sh,.sql,.toml,.ini,.cfg,.go,.rs,.java,.c,.h,.cpp,.hpp,.rb,.php,.swift,.kt,.kts,.scala,.r";

type Props = {
  placeholder: string;
  sending: boolean;
  model: string;
  models?: ModelEntry[];
  modelLocked?: boolean;
  /** 目录尚未读到时不显示固定型号，免得先闪出型号标识。 */
  modelsPending?: boolean;
  onModelChange?: (model: string) => void;
  /** 模型目录读不到最新版本时的提示；沿用旧目录，不阻止发送。 */
  catalogNotice?: { message: string; retrying: boolean; onRetry: () => void } | null;
  onSubmit: (message: string, files: File[]) => Promise<ApiError | null>;
  /** 未发出的消息退回时预填的文字与附件。 */
  initialMessage?: string;
  initialFiles?: File[];
  selection?: Selection;
  onSelectionChange?: (selection: Selection) => void;
};

const formatSize = (size: number) => size >= 1024 * 1024
  ? `${(size / 1024 / 1024).toFixed(1)} MB`
  : `${Math.max(1, Math.round(size / 1024))} KB`;

export default function Composer({
  placeholder, sending, model, models = [], modelLocked = false, modelsPending = false, catalogNotice = null, onModelChange, onSubmit,
  initialMessage = "", initialFiles = [],
  selection, onSelectionChange,
}: Props) {
  const [message, setMessage] = useState(initialMessage);
  const [files, setFiles] = useState<File[]>(initialFiles);
  const [error, setError] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const addArea = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const previews = useRef(new Map<File, string>());

  useEffect(() => () => {
    for (const url of previews.current.values()) URL.revokeObjectURL(url);
  }, []);
  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (addArea.current && !addArea.current.contains(event.target as Node)) {
        setAddOpen(false);
        setSkillsOpen(false);
      }
    };
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setAddOpen(false); setSkillsOpen(false); }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onEscape);
    return () => { document.removeEventListener("pointerdown", onPointerDown); document.removeEventListener("keydown", onEscape); };
  }, []);

  // 预填的多行文字要撑开输入框，与手动输入时一致。
  useEffect(() => { if (initialMessage) resize(); }, []);

  const imageUrl = (file: File) => {
    const known = previews.current.get(file);
    if (known !== undefined) return known;
    const url = URL.createObjectURL(file);
    previews.current.set(file, url);
    return url;
  };

  const resize = () => {
    const node = textarea.current;
    if (node === null) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 180)}px`;
  };

  const addFiles = (selected: File[]) => {
    setError(null);
    if (files.length + selected.length > MAX_FILES) {
      setError(`每条消息最多上传 ${MAX_FILES} 个附件`);
      return;
    }
    const oversized = selected.find((file) => file.size > MAX_FILE_SIZE);
    if (oversized !== undefined) {
      setError(`${oversized.name} 超过 20 MB`);
      return;
    }
    setFiles((current) => [...current, ...selected]);
  };

  const removeFile = (target: File) => {
    const url = previews.current.get(target);
    if (url !== undefined) URL.revokeObjectURL(url);
    previews.current.delete(target);
    setFiles((current) => current.filter((file) => file !== target));
  };

  const submit = async () => {
    const text = message.trim();
    if ((!text && files.length === 0) || sending || !model) return;
    const failure = await onSubmit(text, files);
    if (failure !== null) {
      setError(failure.message);
      return;
    }
    for (const url of previews.current.values()) URL.revokeObjectURL(url);
    previews.current.clear();
    setMessage("");
    setFiles([]);
    setError(null);
    if (textarea.current !== null) textarea.current.style.height = "auto";
  };

  return <div className="codex-composer" ref={addArea}>
    {selection && onSelectionChange && <SkillPicker value={selection} onChange={onSelectionChange}
      open={skillsOpen} onClose={() => setSkillsOpen(false)} disabled={sending} />}
    {files.length > 0 && <div className="composer-files" aria-label="待发送附件">
      {files.map((file, index) => {
        const image = file.type.startsWith("image/");
        return <div className="composer-file" key={`${index}:${file.name}`}>
          <div className="composer-file-preview">
            {image ? <img src={imageUrl(file)} alt="" /> : <FileText size={18} weight="regular" />}
          </div>
          <div className="composer-file-copy">
            <strong title={file.name}>{file.name}</strong><span>{formatSize(file.size)}</span>
          </div>
          <button type="button" onClick={() => removeFile(file)} disabled={sending}
            aria-label={`移除 ${file.name}`}><X size={12} weight="bold" /></button>
        </div>;
      })}
    </div>}

    <textarea ref={textarea} value={message} rows={1} placeholder={placeholder} aria-label="消息"
      disabled={sending} onChange={(event) => { setMessage(event.target.value); resize(); }}
      onKeyDown={(event) => {
        if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); }
      }} />

    {error !== null && <div className="composer-error" role="alert">{error}</div>}

    <div className="composer-toolbar">
      <input ref={input} type="file" multiple accept={ACCEPT} hidden
        onChange={(event) => {
          addFiles(Array.from(event.target.files ?? []));
          event.target.value = "";
        }} />
      <div className="composer-add-area">
        <button type="button" className="composer-add" onClick={() => { setSkillsOpen(false); setAddOpen(!addOpen); }}
          disabled={sending} aria-label="添加内容" aria-expanded={addOpen}><Plus size={18} weight="bold" /></button>
        {addOpen && <div className="composer-add-menu" role="menu">
          {selection && onSelectionChange && <button type="button" role="menuitem" onClick={() => { setAddOpen(false); setSkillsOpen(true); }}><Sparkle size={17} />Skill</button>}
          <button type="button" role="menuitem" onClick={() => { setAddOpen(false); input.current?.click(); }}><FileText size={17} />文件</button>
        </div>}
      </div>

      <div className="composer-spacer" />
      {catalogNotice !== null && !modelLocked && <span className="composer-catalog-notice">
        <span>{catalogNotice.message}</span>
        <button type="button" onClick={catalogNotice.onRetry} disabled={catalogNotice.retrying}>
          {catalogNotice.retrying ? "刷新中…" : "重试"}
        </button>
      </span>}
      {modelLocked
        ? <span className="composer-model-locked" title="该任务的模型已固定">
          {models.find((entry) => entry.id === model)?.label ?? (modelsPending ? "" : model)}
        </span>
        : <ModelPicker value={model} models={models} disabled={sending}
          onChange={(value) => onModelChange?.(value)} />}
      <button type="button" className="composer-send" onClick={() => void submit()}
        disabled={sending || (!message.trim() && files.length === 0) || !model}
        aria-label="发送"><ArrowUp size={16} weight="bold" /></button>
    </div>
  </div>;
}
