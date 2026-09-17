import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  addMemoryEntry,
  getMemory,
  removeMemoryEntry,
  updateMemoryEntry,
  type MemorySection,
  type MemorySnapshot,
  type MemoryTarget,
} from "../api";
import AppShell from "../components/AppShell";
import Notice from "../components/Notice";

const SECTIONS: { target: MemoryTarget; title: string; hint: string }[] = [
  { target: "user", title: "关于你", hint: "背景、长期目标、语言与表达偏好、工作习惯" },
  { target: "memory", title: "事实与约定", hint: "需要每轮生效的约定与经验；可以查的资料放资料库" },
];

/** 容量条从这个比例起提醒快满了。 */
const NEAR_FULL = 0.9;

/** 正在编辑的条目：`original` 为空表示新增。 */
type Editing = { target: MemoryTarget; original: string | null; text: string };
type Removing = { target: MemoryTarget; entry: string };
type Failure = { target: MemoryTarget; error: ApiError };

function asApiError(failure: unknown): ApiError {
  return failure instanceof ApiError ? failure : new ApiError("invalid_request", String(failure), 0);
}

function failureText(error: ApiError): string {
  if (error.code === "memory_full") return `${error.message}。先精简这条，或删除、合并其他条目。`;
  const detail = error.fieldErrors?.map((item) => item.message).join("；");
  return detail ? detail : error.message;
}

function Usage({ section }: { section: MemorySection }) {
  const { chars, limit } = section.usage;
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

type EditorProps = {
  editing: Editing;
  busy: boolean;
  onChange: (text: string) => void;
  onSave: () => void;
  onCancel: () => void;
};

function EntryEditor({ editing, busy, onChange, onSave, onCancel }: EditorProps) {
  const unchanged = editing.text.trim() === "" || editing.text.trim() === editing.original;
  return (
    <form className="memory-editor" onSubmit={(event) => { event.preventDefault(); onSave(); }}>
      <textarea className="input" value={editing.text} autoFocus rows={3}
        onFocus={(event) => {
          // 编辑已有条目时光标放到末尾，接着原话往下改。
          const end = event.currentTarget.value.length;
          event.currentTarget.setSelectionRange(end, end);
        }}
        aria-label={editing.original === null ? "新记忆内容" : "编辑记忆内容"}
        placeholder="一句简短明确的话，涉及条件时写上条件"
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") onCancel();
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            if (!unchanged) onSave();
          }
        }} />
      <div className="memory-editor-actions">
        <button type="submit" className="btn" disabled={busy || unchanged}>{busy ? "保存中…" : "保存"}</button>
        <button type="button" className="btn-secondary" disabled={busy} onClick={onCancel}>取消</button>
      </div>
    </form>
  );
}

/**
 * 长期记忆的查看与编辑：两个分区分别对应 USER.md 与 MEMORY.md。
 *
 * 记忆不做版本管理，每次保存都带上读取时的内容版本：页面打开之后被记忆判断、后台整理
 * 或编辑器改过时保存会被拒绝，提示重新载入，不覆盖别人的修改。修改从下一轮对话起生效。
 */
export default function MemoryPage() {
  const [snapshot, setSnapshot] = useState<MemorySnapshot | null>(null);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [removing, setRemoving] = useState<Removing | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setSnapshot(await getMemory());
      setLoadError(null);
      setFailure(null);
    } catch (error) {
      setLoadError(asApiError(error));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const write = async (target: MemoryTarget, action: (version: string) => Promise<MemorySection>) => {
    if (snapshot === null || busy) return;
    setBusy(true);
    setFailure(null);
    try {
      const section = await action(snapshot[target].version);
      setSnapshot({ ...snapshot, [target]: section });
      setEditing(null);
      setRemoving(null);
    } catch (error) {
      setFailure({ target, error: asApiError(error) });
    } finally {
      setBusy(false);
    }
  };

  const save = () => {
    if (editing === null) return;
    const { target, original } = editing;
    const text = editing.text.trim();
    void write(target, (version) => original === null
      ? addMemoryEntry(target, text, version)
      : updateMemoryEntry(target, original, text, version));
  };

  const remove = () => {
    if (removing === null) return;
    const { target, entry } = removing;
    void write(target, (version) => removeMemoryEntry(target, entry, version));
  };

  const startEditing = (next: Editing) => {
    setEditing(next);
    setRemoving(null);
    setFailure(null);
  };

  const editorProps = (current: Editing): EditorProps => ({
    editing: current,
    busy,
    onChange: (text) => setEditing({ ...current, text }),
    onSave: save,
    onCancel: () => { setEditing(null); setFailure(null); },
  });

  return (
    <AppShell serviceError={loadError}>
      <div className="topbar">
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2>记忆</h2>
          <div className="sub">每轮对话都会带上这些内容，修改从下一轮起生效</div>
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

        {snapshot !== null && SECTIONS.map(({ target, title, hint }) => {
          const section = snapshot[target];
          const sectionFailure = failure?.target === target ? failure.error : null;
          const conflict = sectionFailure?.code === "version_conflict";
          const adding = editing?.target === target && editing.original === null ? editing : null;
          return (
            <section className="memory-section" key={target} aria-labelledby={`memory-${target}`}>
              <div className="memory-head">
                <div className="memory-head-text">
                  <h3 id={`memory-${target}`}>{title}</h3>
                  <div className="memory-hint">{hint}</div>
                </div>
                <Usage section={section} />
              </div>

              {sectionFailure !== null && (
                <Notice tone="danger" title={conflict ? "记忆已被更新" : "保存失败"}
                  actions={conflict
                    ? <button type="button" className="btn-secondary" onClick={() => void load()}>重新载入</button>
                    : undefined}>
                  {conflict
                    ? "页面打开之后，这部分记忆被对话或文件编辑器改过。为避免覆盖，本次没有保存；重新载入后再改。"
                    : failureText(sectionFailure)}
                </Notice>
              )}

              <div className="list-card memory-list">
                {section.entries.length === 0 && adding === null && (
                  <div className="memory-empty">还没有内容。对话中明确说出的偏好会自动记在这里。</div>
                )}
                {section.entries.map((entry) => {
                  const current = editing?.target === target && editing.original === entry ? editing : null;
                  const confirming = removing?.target === target && removing.entry === entry;
                  if (current !== null) {
                    return <div className="memory-entry" key={entry}><EntryEditor {...editorProps(current)} /></div>;
                  }
                  return (
                    <div className="memory-entry" key={entry}>
                      <p className="memory-text">{entry}</p>
                      {confirming ? (
                        <div className="memory-confirm">
                          <span>删除后不再带入对话，原对话仍保留。</span>
                          <button type="button" className="btn danger" disabled={busy} onClick={remove}>确认删除</button>
                          <button type="button" className="btn-secondary" disabled={busy}
                            onClick={() => setRemoving(null)}>取消</button>
                        </div>
                      ) : (
                        <div className="memory-entry-actions">
                          <button type="button" className="memory-action" disabled={busy}
                            onClick={() => startEditing({ target, original: entry, text: entry })}>编辑</button>
                          <button type="button" className="memory-action danger" disabled={busy}
                            onClick={() => { setRemoving({ target, entry }); setEditing(null); setFailure(null); }}>
                            删除
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
                {adding !== null
                  ? <div className="memory-entry"><EntryEditor {...editorProps(adding)} /></div>
                  : (
                    <button type="button" className="memory-add" disabled={busy}
                      onClick={() => startEditing({ target, original: null, text: "" })}>
                      + 新增一条
                    </button>
                  )}
              </div>
            </section>
          );
        })}
      </div>
    </AppShell>
  );
}
