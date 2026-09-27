import { ArrowUp, Cube, FileText, Plus, Square, X } from "@phosphor-icons/react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import type { ApiError, ContextReading, ModelEntry, SkillSelection, SkillSummary } from "../api";
import ContextMeter from "./ContextMeter";
import ModelPicker from "./ModelPicker";

const MAX_FILES = 10;
const MAX_FILE_SIZE = 20 * 1024 * 1024;
const ACCEPT = ".png,.jpg,.jpeg,.webp,.pdf,.txt,.md,.markdown,.py,.js,.jsx,.ts,.tsx,.json,.yaml,.yml,.xml,.csv,.html,.css,.sh,.sql,.toml,.ini,.cfg,.go,.rs,.java,.c,.h,.cpp,.hpp,.rb,.php,.swift,.kt,.kts,.scala,.r";

type Props = {
  placeholder: string;
  sending: boolean;
  /** 有一轮正在执行：右下角按钮换成终止，这一轮里不再发送新消息。 */
  running?: boolean;
  /** 终止请求已发出、还在收尾：按钮短暂不可再点。 */
  stopping?: boolean;
  onStop?: () => Promise<ApiError | null>;
  model: string;
  models?: ModelEntry[];
  modelLocked?: boolean;
  /** 目录尚未读到时不显示固定型号，免得先闪出型号标识。 */
  modelsPending?: boolean;
  onModelChange?: (model: string) => void;
  /** 可选的技能目录；提供时输入 `/` 或从「＋」菜单唤起同一列表。 */
  skills?: SkillSummary[];
  /** 模型目录读不到最新版本时的提示；沿用旧目录，不阻止发送。 */
  catalogNotice?: { message: string; retrying: boolean; onRetry: () => void } | null;
  /** 最近一轮结束时的上下文读数；未传入时不显示标记。 */
  context?: ContextReading | null;
  onSubmit: (message: string, files: File[], selection: SkillSelection) => Promise<ApiError | null>;
  /** 未发出的消息退回时预填的文字与附件。 */
  initialMessage?: string;
  initialFiles?: File[];
};

const formatSize = (size: number) => size >= 1024 * 1024
  ? `${(size / 1024 / 1024).toFixed(1)} MB`
  : `${Math.max(1, Math.round(size / 1024))} KB`;

/** 取光标前文本里正在输入的 `/` 词：`/` 须在开头或空白之后（网址、路径不受影响），词一直延伸到光标。 */
const matchSlashWord = (before: string) => {
  const match = /(^|\s)\/([^\s]*)$/.exec(before);
  return match === null ? null : { start: match.index + match[1].length, query: match[2] };
};

/** 编辑区只接受纯文本与不可编辑的 Skill 节点；提交的正文不包含节点标签。 */
const editorText = (root: HTMLElement): string => {
  let text = "";
  for (const child of root.childNodes) {
    if (child.nodeType === Node.TEXT_NODE) text += child.textContent ?? "";
    else if (child instanceof HTMLElement && child.dataset.skillId) continue;
    else if (child instanceof HTMLBRElement) text += "\n";
    else if (child instanceof HTMLElement) {
      text += editorText(child);
      if (child.tagName === "DIV" && child.nextSibling) text += "\n";
    }
  }
  return text;
};

const selectedSkillIds = (root: HTMLElement): string[] =>
  Array.from(root.querySelectorAll<HTMLElement>("[data-skill-id]"), (node) => node.dataset.skillId!);

const caretWord = (root: HTMLElement) => {
  const selection = window.getSelection();
  if (!selection || !selection.isCollapsed || !root.contains(selection.anchorNode)) return null;
  const node = selection.anchorNode;
  if (node?.nodeType !== Node.TEXT_NODE || node.parentElement?.closest("[data-skill-id]")) return null;
  const offset = selection.anchorOffset;
  const match = matchSlashWord((node.textContent ?? "").slice(0, offset));
  return match === null ? null : { node, start: match.start, end: offset, query: match.query };
};

const makeSkillNode = (skill: SkillSummary) => {
  const node = document.createElement("span");
  node.className = "composer-inline-skill";
  node.contentEditable = "false";
  node.dataset.skillId = skill.skill_id;
  node.setAttribute("aria-label", `技能 ${skill.name}`);
  node.title = skill.description;
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("viewBox", "0 0 256 256");
  icon.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("fill", "currentColor");
  path.setAttribute("d", "M248 152a8 8 0 0 1-8 8h-16v16a8 8 0 0 1-16 0v-16h-16a8 8 0 0 1 0-16h16v-16a8 8 0 0 1 16 0v16h16a8 8 0 0 1 8 8ZM56 72h16v16a8 8 0 0 0 16 0V72h16a8 8 0 0 0 0-16H88V40a8 8 0 0 0-16 0v16H56a8 8 0 0 0 0 16Zm128 120h-8v-8a8 8 0 0 0-16 0v8h-8a8 8 0 0 0 0 16h8v8a8 8 0 0 0 16 0v-8h8a8 8 0 0 0 0-16ZM219.31 80 80 219.31a16 16 0 0 1-22.62 0l-20.7-20.68a16 16 0 0 1 0-22.63L176 36.69a16 16 0 0 1 22.63 0l20.68 20.68A16 16 0 0 1 219.31 80Zm-54.63 32L144 91.31l-96 96L68.68 208ZM208 68.69 187.31 48l-32 32L176 100.69Z");
  icon.append(path);
  node.append(icon);
  const name = document.createElement("span");
  name.textContent = skill.name;
  node.append(name);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.tabIndex = -1;
  remove.setAttribute("aria-label", `移除技能 ${skill.name}`);
  remove.textContent = "×";
  node.append(remove);
  return node;
};

export default function Composer({
  placeholder, sending, running = false, stopping = false, onStop, model, models = [], modelLocked = false,
  modelsPending = false, catalogNotice = null, onModelChange,
  skills, context, onSubmit, initialMessage = "", initialFiles = [],
}: Props) {
  const [message, setMessage] = useState(initialMessage);
  const [files, setFiles] = useState<File[]>(initialFiles);
  const [pickedIds, setPickedIds] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  // `/` 列表用 Esc 收起后，同一个 `/` 词不再自动弹出；光标离开该词再回来才恢复。
  const [dismissed, setDismissed] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const [addOpen, setAddOpen] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const editor = useRef<HTMLDivElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const addArea = useRef<HTMLDivElement>(null);
  const addButton = useRef<HTMLButtonElement>(null);
  const lastCaret = useRef<Range | null>(null);
  const previews = useRef(new Map<File, string>());
  // 输入法组合标志：Safari 提交组合的回车发出时 isComposing 已复位，
  // 所以组合结束的复位推迟一个宏任务，让那一次回车仍被认成组合按键。
  const composing = useRef(false);
  const composingReset = useRef<number | null>(null);

  useEffect(() => () => {
    for (const url of previews.current.values()) URL.revokeObjectURL(url);
  }, []);

  const [slashQuery, setSlashQuery] = useState<string | null>(null);
  const syncEditor = () => {
    const node = editor.current;
    if (!node) return;
    const selection = window.getSelection();
    if (selection?.isCollapsed && node.contains(selection.anchorNode) && selection.rangeCount > 0) {
      lastCaret.current = selection.getRangeAt(0).cloneRange();
    }
    setMessage(editorText(node));
    setPickedIds(selectedSkillIds(node));
    setSlashQuery(caretWord(node)?.query ?? null);
  };

  // 光标处的 `/` 词就是过滤词，词变了高亮从头开始。
  useEffect(() => { setHighlight(0); }, [slashQuery]);
  // 收起是粘性的：点外面或 Esc 之后，同一个 `/` 词继续输入不再弹出；光标离开该词（或删掉它）再回来才恢复。
  useEffect(() => { if (slashQuery === null) setDismissed(false); }, [slashQuery]);

  const imageUrl = (file: File) => {
    const known = previews.current.get(file);
    if (known !== undefined) return known;
    const url = URL.createObjectURL(file);
    previews.current.set(file, url);
    return url;
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

  const slashOpen = skills !== undefined && slashQuery !== null && !dismissed;
  // 已选中的不再进列表（可从行内节点移除），按名称或标识不区分大小写过滤。
  const candidates = slashOpen && skills !== undefined ? skills.filter((skill) =>
    !pickedIds.includes(skill.skill_id)
    && [skill.name, skill.skill_id].some((text) => text.toLowerCase().includes(slashQuery!.toLowerCase())),
  ) : [];
  const active = Math.min(highlight, candidates.length - 1);

  // 列表开着时，点它以外的任何地方都收起（点选项本身不收，走选中流程）。
  useEffect(() => {
    if (!slashOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (menu.current === null || !menu.current.contains(event.target as Node)) setDismissed(true);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [slashOpen]);

  useEffect(() => {
    if (!addOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!addArea.current?.contains(event.target as Node)) setAddOpen(false);
    };
    const onEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setAddOpen(false);
      addButton.current?.focus();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onEscape);
    };
  }, [addOpen]);

  /** 从「＋」入口在原光标处插入 `/`，交给现有技能列表处理。 */
  const openSkillsFromAdd = () => {
    setAddOpen(false);
    const root = editor.current;
    if (!root) return;
    const saved = lastCaret.current;
    const range = saved && root.contains(saved.startContainer) ? saved.cloneRange() : document.createRange();
    if (!saved || !root.contains(saved.startContainer)) {
      range.selectNodeContents(root);
      range.collapse(false);
    }
    const before = range.cloneRange();
    before.selectNodeContents(root);
    before.setEnd(range.startContainer, range.startOffset);
    const separator = before.toString() && !/\s$/.test(before.toString()) ? " " : "";
    const slash = document.createTextNode(`${separator}/`);
    range.insertNode(slash);
    range.setStart(slash, slash.length);
    range.collapse(true);
    root.focus();
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    setDismissed(false);
    syncEditor();
  };

  /** 用行内不可编辑节点替换光标前的 `/` 词，光标继续留在它后面。 */
  const pickSkill = (skill: SkillSummary) => {
    const root = editor.current;
    const word = root && caretWord(root);
    if (!root || !word) return;
    const range = document.createRange();
    range.setStart(word.node, word.start);
    range.setEnd(word.node, word.end);
    range.deleteContents();
    const mention = makeSkillNode(skill);
    range.insertNode(mention);
    // 浏览器需要一个可编辑的文本节点，才能把光标放在 Skill 后继续输入。
    const after = document.createTextNode("");
    mention.after(after);
    const selection = window.getSelection();
    range.setStart(after, 0);
    range.collapse(true);
    selection?.removeAllRanges();
    selection?.addRange(range);
    root.focus();
    syncEditor();
    setDismissed(false);
  };

  const removeSkill = (id: string) => {
    Array.from(editor.current?.querySelectorAll<HTMLElement>("[data-skill-id]") ?? [])
      .find((node) => node.dataset.skillId === id)?.remove();
    syncEditor();
    editor.current?.focus();
  };

  const submit = async () => {
    const text = message.trim();
    // 执行中不发送新消息：右下角那个位置是终止按钮，回车与它保持一致。
    if ((!text && files.length === 0) || sending || running || !model) return;
    const selection: SkillSelection = {
      skills: pickedIds.map((id) => ({ id })), excluded_skill_ids: [], auto_match: true,
    };
    const failure = await onSubmit(text, files, selection);
    if (failure !== null) {
      setError(failure.message);
      return;
    }
    for (const url of previews.current.values()) URL.revokeObjectURL(url);
    previews.current.clear();
    setMessage("");
    setFiles([]);
    setPickedIds([]);
    if (editor.current) editor.current.replaceChildren();
    lastCaret.current = null;
    setAddOpen(false);
    setSlashQuery(null);
    setError(null);
  };

  const stop = async () => {
    setError(null);
    const failure = await onStop?.();
    if (failure != null) setError(failure.message);
  };

  const onCompositionStart = () => {
    // 新组合开始时取消上一次结束的延迟复位，连续组合不会把标志提前清掉。
    if (composingReset.current !== null) window.clearTimeout(composingReset.current);
    composingReset.current = null;
    composing.current = true;
  };

  const onCompositionEnd = () => {
    composingReset.current = window.setTimeout(() => {
      composing.current = false;
      composingReset.current = null;
    }, 0);
  };

  const onComposerKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    // 输入法组合中的按键（选词、提交组合用的回车）不拦截，避免误当列表导航或发送。
    if (composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    if (slashOpen) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setHighlight(Math.min(active + 1, candidates.length - 1));
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setHighlight(Math.max(active - 1, 0));
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setDismissed(true);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        // 没有可选项时 Enter 只收起列表：`/xxx` 多半是误触，不该当正文发出去。
        if (candidates.length > 0) {
          event.preventDefault();
          pickSkill(candidates[active]);
        } else if (event.key === "Enter") {
          event.preventDefault();
          setDismissed(true);
        }
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); }
  };

  return <div className="codex-composer">
    {slashOpen && <div ref={menu} className="composer-skill-menu" role="listbox" aria-label="技能列表">
      <div className="composer-skill-menu-title">技能</div>
      {candidates.length === 0 && <div className="composer-skill-empty">
        {skills !== undefined && skills.length === 0 ? "还没有启用中的技能，可在“技能”页创建" : "无匹配技能"}
      </div>}
      {candidates.map((skill, index) => (
        <button type="button" role="option" aria-selected={index === active} key={skill.skill_id}
          className={`composer-skill-option${index === active ? " active" : ""}`}
          // 按下不转移焦点，输入框继续接收后续按键；鼠标移动把高亮带过去，回车选中的就是悬停项。
          onMouseDown={(event) => event.preventDefault()}
          onMouseMove={() => setHighlight(index)}
          onClick={() => pickSkill(skill)}
          ref={(node) => { if (index === active && node !== null) node.scrollIntoView({ block: "nearest" }); }}>
          <Cube size={15} weight="duotone" />
          <span className="composer-skill-name">{skill.name}</span>
          <span className="composer-skill-desc">{skill.description}</span>
        </button>
      ))}
    </div>}

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

    <div ref={editor} contentEditable={!sending} role="textbox" aria-label="消息" aria-multiline="true"
      data-placeholder={placeholder} className="composer-editor" suppressContentEditableWarning
      onInput={syncEditor}
      onMouseUp={syncEditor}
      onClick={(event) => {
        const target = event.target as HTMLElement;
        const remove = target.closest<HTMLButtonElement>(".composer-inline-skill button");
        if (remove && !sending) removeSkill(remove.parentElement!.dataset.skillId!);
        else syncEditor();
      }}
      onKeyUp={syncEditor}
      onPaste={(event) => {
        event.preventDefault();
        document.execCommand("insertText", false, event.clipboardData.getData("text/plain"));
      }}
      onDrop={(event) => {
        event.preventDefault();
        const text = event.dataTransfer.getData("text/plain");
        if (text) document.execCommand("insertText", false, text);
      }}
      onCompositionStart={onCompositionStart} onCompositionEnd={onCompositionEnd}
      onKeyDown={onComposerKeyDown}>{initialMessage}</div>

    {error !== null && <div className="composer-error" role="alert">{error}</div>}

    <div className="composer-toolbar">
      <input ref={input} type="file" multiple accept={ACCEPT} hidden
        onChange={(event) => {
          addFiles(Array.from(event.target.files ?? []));
          event.target.value = "";
        }} />
      <div ref={addArea} className="composer-add-area">
        <button ref={addButton} type="button" className="composer-add"
          onClick={() => { setDismissed(true); setAddOpen((open) => !open); }}
          disabled={sending} aria-label="添加内容" aria-haspopup="menu" aria-expanded={addOpen}>
          <Plus size={18} weight="bold" />
        </button>
        {addOpen && <div className="composer-add-menu" role="menu" aria-label="添加内容">
          {skills !== undefined && <button type="button" role="menuitem" className="composer-add-option"
            onClick={openSkillsFromAdd}><Cube size={16} weight="duotone" />技能</button>}
          <button type="button" role="menuitem" className="composer-add-option"
            onClick={() => { setAddOpen(false); input.current?.click(); }}>
            <FileText size={16} weight="regular" />文件
          </button>
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
      {context !== undefined && <ContextMeter reading={context} />}
      {running
        ? <button type="button" className="composer-stop" onClick={() => void stop()}
          disabled={stopping} aria-label={stopping ? "正在终止" : "终止"} title="终止这一轮">
          <Square size={13} weight="fill" />
        </button>
        : <button type="button" className="composer-send" onClick={() => void submit()}
          disabled={sending || (!message.trim() && files.length === 0) || !model}
          aria-label="发送"><ArrowUp size={16} weight="bold" /></button>}
    </div>
  </div>;
}
