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
  /** 可选的技能目录；提供时输入 `/` 唤起技能列表，选中项随每条消息提交并重置。 */
  skills?: SkillSummary[];
  /** 模型目录读不到最新版本时的提示；沿用旧目录，不阻止发送。 */
  catalogNotice?: { message: string; retrying: boolean; onRetry: () => void } | null;
  /** 最近一轮结束时的上下文读数；发送按钮左侧的标记按它显示。 */
  context?: ContextReading | null;
  onSubmit: (message: string, files: File[], selection: SkillSelection) => Promise<ApiError | null>;
  /** 未发出的消息退回时预填的文字与附件。 */
  initialMessage?: string;
  initialFiles?: File[];
};

const formatSize = (size: number) => size >= 1024 * 1024
  ? `${(size / 1024 / 1024).toFixed(1)} MB`
  : `${Math.max(1, Math.round(size / 1024))} KB`;

export default function Composer({
  placeholder, sending, running = false, stopping = false, onStop, model, models = [], modelLocked = false,
  modelsPending = false, catalogNotice = null, onModelChange,
  skills, context = null, onSubmit, initialMessage = "", initialFiles = [],
}: Props) {
  const [message, setMessage] = useState(initialMessage);
  const [files, setFiles] = useState<File[]>(initialFiles);
  const [selection, setSelection] = useState<SkillSelection>({
    skills: [], excluded_skill_ids: [], auto_match: true,
  });
  const [error, setError] = useState<string | null>(null);
  // `/` 列表用 Esc 收起后，同一条 `/` 文本不再自动弹出，继续输入才重新出现。
  const [dismissed, setDismissed] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const previews = useRef(new Map<File, string>());

  useEffect(() => () => {
    for (const url of previews.current.values()) URL.revokeObjectURL(url);
  }, []);

  // 预填的多行文字要撑开输入框，与手动输入时一致。
  useEffect(() => { if (initialMessage) resize(); }, []);

  // `/` 后的文本就是过滤词，词变了高亮从头开始。
  const slashQuery = /^\/(\S*)$/.exec(message)?.[1] ?? null;
  useEffect(() => { setHighlight(0); }, [slashQuery]);
  // 收起是粘性的：点外面或 Esc 之后，同一段 `/` 文本继续输入不再弹出；清掉 `/` 重打才恢复。
  useEffect(() => { if (slashQuery === null) setDismissed(false); }, [slashQuery]);

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

  const slashOpen = skills !== undefined && slashQuery !== null && !dismissed;
  const pickedIds = selection.skills.map((pick) => pick.id);
  // 已选中的不再进列表（移除走胶囊上的 ×），按名称或标识不区分大小写过滤。
  const candidates = slashOpen && skills !== undefined ? skills.filter((skill) =>
    !pickedIds.includes(skill.skill_id)
    && [skill.name, skill.skill_id].some((text) => text.toLowerCase().includes(slashQuery!.toLowerCase())),
  ) : [];
  const active = Math.min(highlight, candidates.length - 1);
  const pickedSkills = (skills ?? []).filter((skill) => pickedIds.includes(skill.skill_id));

  // 列表开着时，点它以外的任何地方都收起（点选项本身不收，走选中流程）。
  useEffect(() => {
    if (!slashOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (menu.current === null || !menu.current.contains(event.target as Node)) setDismissed(true);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [slashOpen]);

  /** 选中即从输入框拿掉 `/` 文本，挂成胶囊；列表随文本清空自然收起。 */
  const pickSkill = (skill: SkillSummary) => {
    setSelection((current) => ({ ...current, skills: [...current.skills, { id: skill.skill_id }] }));
    setMessage("");
    setDismissed(false);
    if (textarea.current !== null) textarea.current.style.height = "auto";
  };

  const removeSkill = (id: string) => setSelection((current) => ({
    ...current, skills: current.skills.filter((pick) => pick.id !== id),
  }));

  const submit = async () => {
    const text = message.trim();
    // 执行中不发送新消息：右下角那个位置是终止按钮，回车与它保持一致。
    if ((!text && files.length === 0) || sending || running || !model) return;
    const failure = await onSubmit(text, files, selection);
    if (failure !== null) {
      setError(failure.message);
      return;
    }
    for (const url of previews.current.values()) URL.revokeObjectURL(url);
    previews.current.clear();
    setMessage("");
    setFiles([]);
    setSelection({ skills: [], excluded_skill_ids: [], auto_match: true });
    setError(null);
    if (textarea.current !== null) textarea.current.style.height = "auto";
  };

  const stop = async () => {
    setError(null);
    const failure = await onStop?.();
    if (failure != null) setError(failure.message);
  };

  const onComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
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

    {pickedSkills.length > 0 && <div className="composer-skills" aria-label="待发送技能">
      {pickedSkills.map((skill) => (
        <div className="composer-skill" key={skill.skill_id}>
          <Cube size={13} weight="fill" />
          <strong title={skill.description}>{skill.name}</strong>
          <button type="button" onClick={() => removeSkill(skill.skill_id)} disabled={sending}
            aria-label={`移除技能 ${skill.name}`}><X size={12} weight="bold" /></button>
        </div>
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

    <textarea ref={textarea} value={message} rows={1} placeholder={placeholder} aria-label="消息"
      disabled={sending} onChange={(event) => { setMessage(event.target.value); resize(); }}
      onKeyDown={onComposerKeyDown} />

    {error !== null && <div className="composer-error" role="alert">{error}</div>}

    <div className="composer-toolbar">
      <input ref={input} type="file" multiple accept={ACCEPT} hidden
        onChange={(event) => {
          addFiles(Array.from(event.target.files ?? []));
          event.target.value = "";
        }} />
      <button type="button" className="composer-add" onClick={() => input.current?.click()}
        disabled={sending} aria-label="添加图片或文件"><Plus size={18} weight="bold" /></button>

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
      <ContextMeter reading={context} />
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
