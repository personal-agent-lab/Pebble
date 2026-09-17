import { useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  getMemory,
  saveMemory,
  type MemorySection,
  type MemorySnapshot,
  type MemoryTarget,
} from "../api";
import AppShell from "../components/AppShell";
import KbEditor from "../components/KbEditor";
import Notice from "../components/Notice";

const SECTIONS: { target: MemoryTarget; title: string }[] = [
  { target: "user", title: "关于你" },
  { target: "memory", title: "事实与约定" },
];

/** 容量条从这个比例起提醒快满了。 */
const NEAR_FULL = 0.9;

function asApiError(failure: unknown): ApiError {
  return failure instanceof ApiError ? failure : new ApiError("invalid_request", String(failure), 0);
}

function failureText(error: ApiError): string {
  if (error.code === "memory_full") return `${error.message}。先精简或合并已有内容再保存。`;
  const detail = error.fieldErrors?.map((item) => item.message).join("；");
  return detail ? detail : error.message;
}

/**
 * 保存前的整理，与服务端的规范化一致：统一换行、去掉首尾空白、合并连续空行，容量按它计算。
 *
 * 编辑器把空段落序列化成独占一行的 `<br />`：它对 Agent 没有意义，保存时当作空行去掉。
 */
function normalize(content: string): string {
  return content
    .replace(/\r\n/g, "\n")
    .replace(/^[ \t]*<br\s*\/?>[ \t]*$/gm, "")
    .trim()
    .replace(/\n[ \t]*\n(?:[ \t]*\n)+/g, "\n\n");
}

function Usage({ chars, limit }: { chars: number; limit: number }) {
  const ratio = limit > 0 ? chars / limit : 0;
  const tone = ratio > 1 ? " over" : ratio >= NEAR_FULL ? " near" : "";
  return (
    <div className={`memory-usage${tone}`}>
      <div className="memory-meter" role="meter" aria-label="已用容量"
        aria-valuemin={0} aria-valuemax={limit} aria-valuenow={chars}>
        <span style={{ width: `${Math.min(ratio, 1) * 100}%` }} />
      </div>
      <span className="memory-usage-text">
        {ratio > 1 ? `超出上限 ${chars - limit} 字 · ` : ""}{chars} / {limit} 字
      </span>
    </div>
  );
}

type DocumentProps = {
  target: MemoryTarget;
  title: string;
  section: MemorySection;
  onSaved: (section: MemorySection) => void;
  onReload: () => void;
  onDirty: (target: MemoryTarget, dirty: boolean) => void;
};

/**
 * 一块记忆的所见即所得编辑区：渲染后的 Markdown 直接可改，有改动时出现保存。
 *
 * 编辑器会按自己的写法重新排版原文，“有改动”以编辑器载入后序列化的基准为准，
 * 只打开不编辑不会产生保存。
 */
function MemoryDocument({ target, title, section, onSaved, onReload, onDirty }: DocumentProps) {
  const baseline = useRef<string | null>(null);
  const reader = useRef<(() => string) | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const [editorKey, setEditorKey] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const dirty = touched || (baseline.current !== null && draft !== null && draft !== baseline.current);
  useEffect(() => { onDirty(target, dirty); }, [target, dirty, onDirty]);
  useEffect(() => () => onDirty(target, false), [target, onDirty]);

  const chars = dirty && draft !== null ? normalize(draft).length : section.usage.chars;
  const over = chars > section.usage.limit && chars > section.usage.chars;

  const reset = () => {
    baseline.current = null;
    setDraft(null);
    setTouched(false);
    setError(null);
    setEditorKey((key) => key + 1);
  };

  const save = async () => {
    if (saving || !dirty) return;
    const latest = reader.current?.() ?? draft ?? section.content;
    setSaving(true);
    setError(null);
    setNote(null);
    try {
      if (normalize(latest) === normalize(baseline.current ?? section.content)) {
        setTouched(false);
        setDraft(baseline.current);
        setNote("没有需要保存的改动");
        return;
      }
      const saved = await saveMemory(target, normalize(latest), section.version);
      onSaved(saved);
      reset();
      setNote("已保存，从下一轮对话起生效");
    } catch (failure) {
      setError(asApiError(failure));
    } finally {
      setSaving(false);
    }
  };

  const conflict = error?.code === "version_conflict";

  return (
    <section className="memory-section" aria-labelledby={`memory-${target}`}>
      <div className="memory-head">
        <h3 id={`memory-${target}`}>{title}</h3>
        <Usage chars={chars} limit={section.usage.limit} />
      </div>

      {error !== null && (
        <Notice tone="danger" title={conflict ? "记忆已被更新" : "保存失败"}
          actions={conflict
            ? <button type="button" className="btn-secondary" onClick={onReload}>重新载入（放弃本页修改）</button>
            : undefined}>
          {conflict
            ? "页面打开之后，这部分记忆被对话或文件编辑器改过。为避免覆盖，本次没有保存。"
            : failureText(error)}
        </Notice>
      )}

      <div
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            void save();
          }
        }}
      >
        <KbEditor
          key={`${section.version}:${editorKey}`}
          className="memory-editor"
          label={`${title}的记忆内容`}
          initial={section.content}
          onReady={(markdown) => {
            baseline.current = markdown;
            setDraft(markdown);
          }}
          onChange={setDraft}
          onInput={() => { setTouched(true); setNote(null); }}
          reader={reader}
        />
      </div>

      {(dirty || note !== null) && (
        <div className="memory-actions">
          {dirty ? (
            <>
              {over && <span className="memory-over">超出上限，先精简再保存</span>}
              <button type="button" className="btn-secondary" disabled={saving} onClick={reset}>放弃修改</button>
              <button type="button" className="btn" disabled={saving || over} onClick={() => void save()}>
                {saving ? "保存中…" : "保存"}
              </button>
            </>
          ) : (
            <span className="memory-note" role="status">{note}</span>
          )}
        </div>
      )}
    </section>
  );
}

/**
 * 长期记忆的查看与编辑：两个分区分别对应 USER.md 与 MEMORY.md，各是一份 Markdown 文档。
 *
 * 记忆不做版本管理，保存带上读取时的内容版本：页面打开之后被记忆判断、后台整理
 * 或编辑器改过时保存会被拒绝，提示重新载入，不覆盖别人的修改。修改从下一轮对话起生效。
 */
export default function MemoryPage() {
  const [snapshot, setSnapshot] = useState<MemorySnapshot | null>(null);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [loads, setLoads] = useState(0);
  const dirtyTargets = useRef(new Set<MemoryTarget>());
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async () => {
    try {
      setSnapshot(await getMemory());
      setLoadError(null);
      setLoads((count) => count + 1);
    } catch (error) {
      setLoadError(asApiError(error));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const onDirty = useCallback((target: MemoryTarget, value: boolean) => {
    if (value) dirtyTargets.current.add(target);
    else dirtyTargets.current.delete(target);
    setDirty(dirtyTargets.current.size > 0);
  }, []);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  return (
    <AppShell serviceError={loadError}>
      <div className="topbar">
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2>记忆</h2>
        </div>
      </div>
      <div className="content memory-content">
        {loadError !== null && (
          <Notice tone="danger" title="读取记忆失败"
            actions={<button type="button" className="btn-secondary" onClick={() => void load()}>重试</button>}>
            {loadError.message}
          </Notice>
        )}

        {snapshot === null && loadError === null && <div className="loading">读取中…</div>}

        {snapshot !== null && SECTIONS.map(({ target, title }) => (
          <MemoryDocument
            // 重新载入时整块重建：丢掉草稿与错误，编辑器换成最新内容。
            key={`${target}:${loads}`}
            target={target}
            title={title}
            section={snapshot[target]}
            onSaved={(section) => setSnapshot((current) => current && { ...current, [target]: section })}
            onReload={() => void load()}
            onDirty={onDirty}
          />
        ))}
      </div>
    </AppShell>
  );
}
