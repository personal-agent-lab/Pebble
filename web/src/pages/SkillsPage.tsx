import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  ApiError,
  archiveSkill,
  approveSkillChange,
  createSkill,
  getSkill,
  getSkillVersion,
  listSkillChanges,
  listSkillVersions,
  listSkills,
  readSkillFile,
  rejectSkillChange,
  removeSkillFile,
  restoreSkill,
  restoreSkillVersion,
  setSkillManaged,
  updateSkill,
  writeSkillFile,
  type SkillChangeView,
  type SkillDetail,
  type SkillState,
  type SkillSummary,
  type SkillVersion,
} from "../api";
import AppShell from "../components/AppShell";
import Notice from "../components/Notice";
import { shortTime } from "../status";

const ORIGIN_LABELS: Record<string, string> = {
  user: "管理页手写",
  explicit: "对话沉淀",
  review: "后台复盘",
};

const TABS: { state: SkillState; label: string }[] = [
  { state: "active", label: "启用中" },
  { state: "archived", label: "已归档" },
];

function asApiError(failure: unknown): ApiError {
  return failure instanceof ApiError ? failure : new ApiError("invalid_request", String(failure), 0);
}

function failureText(error: ApiError): string {
  const detail = error.fieldErrors?.map((item) => item.message).join("；");
  return detail ? detail : error.message;
}

/** 变更载荷的一行预览：按动作挑出关键字段，其余原样。 */
function payloadPreview(change: SkillChangeView): string {
  const payload = change.payload;
  if (change.action === "patch") {
    if (typeof payload.body === "string") return `整份替换正文（${payload.body.length} 字）`;
    return `${String(payload.old_string)} → ${String(payload.new_string)}`;
  }
  if (change.action === "create") return `新建 ${String(payload.skill_id)}`;
  return String(payload.relative_path ?? "");
}

/**
 * 技能管理页：目录、创建与编辑、归档恢复、附件、历史版本与待审变更。
 * 编辑基于读取时的内容版本，过期保存返回 409，提示重新载入而不覆盖。
 */
export default function SkillsPage() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("id");
  const creating = params.get("new") === "1";
  const state = (params.get("state") as SkillState) ?? "active";
  const [entries, setEntries] = useState<SkillSummary[] | null>(null);
  const [changes, setChanges] = useState<SkillChangeView[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    try {
      const [listed, pending] = await Promise.all([
        listSkills(TABS.some((tab) => tab.state === state) ? state : "active"),
        listSkillChanges("proposed").catch(() => []),
      ]);
      setEntries(listed);
      setChanges(pending);
      setError(null);
    } catch (failure) {
      setError(asApiError(failure));
    }
  }, [state]);

  useEffect(() => { void load(); }, [load]);

  const open = (skillId: string | null) => setParams(skillId ? { id: skillId } : {});

  return (
    <AppShell serviceError={error}>
      <div className="topbar">
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2>技能</h2>
        </div>
        <button type="button" className="btn" onClick={() => setParams({ new: "1" })}>新建技能</button>
      </div>
      <div className="content skills-content">
        {error !== null && (
          <Notice tone="danger" title="读取技能失败"
            actions={<button type="button" className="btn-secondary" onClick={() => void load()}>重试</button>}>
            {error.message}
          </Notice>
        )}

        {creating && <CreatePane onDone={() => { open(null); void load(); }} onCancel={() => open(null)} />}
        {!creating && selected !== null && (
          <DetailPane key={selected} skillId={selected} onChanged={load} onBack={() => open(null)} />
        )}
        {!creating && selected === null && (
          <>
            <div className="skills-tabs" role="tablist" aria-label="技能状态">
              {TABS.map((tab) => (
                <button type="button" key={tab.state} role="tab"
                  aria-selected={state === tab.state}
                  className={`skills-tab${state === tab.state ? " active" : ""}`}
                  onClick={() => setParams(tab.state === "active" ? {} : { state: tab.state })}>
                  {tab.label}
                </button>
              ))}
            </div>

            {entries === null && error === null && <div className="loading">读取中…</div>}
            {entries !== null && entries.length === 0 && (
              <div className="empty">
                <div className="empty-title">{state === "archived" ? "没有已归档的技能" : "还没有技能"}</div>
                <div className="empty-sub">手动新建，或在对话里让 Agent 把做法沉淀成技能。</div>
              </div>
            )}
            {entries !== null && entries.length > 0 && (
              <div className="skills-list">
                {entries.map((entry) => (
                  <button type="button" className="skill-row" key={entry.skill_id}
                    onClick={() => open(entry.skill_id)}>
                    <div className="skill-row-copy">
                      <strong>{entry.name}</strong>
                      <span>{entry.description}</span>
                    </div>
                    <div className="skill-row-meta">
                      <span className="skill-origin">{ORIGIN_LABELS[entry.origin] ?? entry.origin}</span>
                      {entry.managed && <span className="skill-managed-flag">复盘可改</span>}
                      <span>{shortTime(entry.updated_at)}</span>
                    </div>
                  </button>
                ))}
              </div>
            )}

            {changes !== null && changes.length > 0 && <ChangesPane changes={changes} onChanged={load} />}
          </>
        )}
      </div>
    </AppShell>
  );
}

/** 新建 `origin=user` 技能；附件按文本读入，放在 references/ 下。 */
function CreatePane({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [fields, setFields] = useState({ skill_id: "", name: "", description: "", body: "" });
  const [attachments, setAttachments] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const addAttachment = async (files: FileList | null) => {
    if (files === null || files.length === 0) return;
    const next = { ...attachments };
    for (const file of Array.from(files)) next[`references/${file.name}`] = await file.text();
    setAttachments(next);
  };

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    try {
      await createSkill({ ...fields, attachments: Object.keys(attachments).length > 0 ? attachments : undefined });
      onDone();
    } catch (error) {
      setFailure(asApiError(error));
      setBusy(false);
    }
  };

  const valid = fields.skill_id.trim() !== "" && fields.name.trim() !== ""
    && fields.description.trim() !== "" && fields.body.trim() !== "";

  return (
    <section className="skill-pane" aria-labelledby="skill-create-title">
      <h3 id="skill-create-title">新建技能</h3>
      {failure !== null && <Notice tone="danger" title="创建失败">{failureText(failure)}</Notice>}
      <label className="skill-field">
        <span>标识</span>
        <input className="input" value={fields.skill_id} placeholder="小写字母数字与连字符，如 weekly-report"
          onChange={(event) => setFields({ ...fields, skill_id: event.target.value })} />
      </label>
      <label className="skill-field">
        <span>名称</span>
        <input className="input" value={fields.name}
          onChange={(event) => setFields({ ...fields, name: event.target.value })} />
      </label>
      <label className="skill-field">
        <span>一句话描述</span>
        <input className="input" value={fields.description} maxLength={160}
          placeholder="给模型判断相关性用，≤160 字符"
          onChange={(event) => setFields({ ...fields, description: event.target.value })} />
      </label>
      <label className="skill-field">
        <span>正文</span>
        <textarea className="input skill-body-input" rows={10} value={fields.body}
          placeholder="适用场景 / 前置条件 / 操作步骤 / 验证方法 / 常见问题"
          onChange={(event) => setFields({ ...fields, body: event.target.value })} />
      </label>
      <div className="skill-attachments">
        <span className="skill-field-label">附件（文本，放 references/）</span>
        <input ref={fileInput} type="file" multiple hidden
          onChange={(event) => { void addAttachment(event.target.files); event.target.value = ""; }} />
        <button type="button" className="btn-secondary" onClick={() => fileInput.current?.click()}>添加附件</button>
        {Object.keys(attachments).map((path) => (
          <span className="skill-attachment-chip" key={path}>
            {path}
            <button type="button" aria-label={`移除 ${path}`}
              onClick={() => {
                const next = { ...attachments };
                delete next[path];
                setAttachments(next);
              }}>×</button>
          </span>
        ))}
      </div>
      <div className="skill-pane-actions">
        <button type="button" className="btn-secondary" onClick={onCancel}>取消</button>
        <button type="button" className="btn" disabled={busy || !valid}
          onClick={() => void submit()}>{busy ? "创建中…" : "创建"}</button>
      </div>
    </section>
  );
}

/** 详情与编辑：编辑带读取时版本，409 提示重新载入；附件、历史版本与状态切换都在这里。 */
function DetailPane({ skillId, onChanged, onBack }: {
  skillId: string; onChanged: () => void; onBack: () => void;
}) {
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [draft, setDraft] = useState<{ name: string; description: string; body: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [versions, setVersions] = useState<SkillVersion[] | null>(null);
  const [viewing, setViewing] = useState<{ revision: string; body: string } | null>(null);
  const [fileView, setFileView] = useState<{ path: string; content: string } | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const reload = useCallback(async () => {
    try {
      const loaded = await getSkill(skillId);
      setDetail(loaded);
      setDraft(null);
      setError(null);
    } catch (failure) {
      setError(asApiError(failure));
    }
  }, [skillId]);

  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => {
    listSkillVersions(skillId).then(setVersions).catch(() => setVersions(null));
  }, [skillId, detail?.revision]);

  const conflict = failure?.code === "skill_conflict";
  const dirty = detail !== null && draft !== null
    && (draft.name !== detail.name || draft.description !== detail.description || draft.body !== detail.body);

  const save = async () => {
    if (busy || !dirty || detail === null || draft === null) return;
    setBusy(true);
    setFailure(null);
    setNote(null);
    try {
      await updateSkill(skillId, detail.revision, draft);
      await reload();
      onChanged();
      setNote("已保存");
    } catch (error) {
      setFailure(asApiError(error));
    } finally {
      setBusy(false);
    }
  };

  const act = async (action: () => Promise<unknown>, message: string) => {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    setNote(null);
    try {
      await action();
      await reload();
      onChanged();
      setNote(message);
    } catch (error) {
      setFailure(asApiError(error));
    } finally {
      setBusy(false);
    }
  };

  const upload = async (files: FileList | null) => {
    if (files === null || files.length === 0 || detail === null || uploading) return;
    setUploading(true);
    setFailure(null);
    setNote(null);
    try {
      for (const file of Array.from(files)) {
        await writeSkillFile(skillId, `references/${file.name}`, await file.text(), detail.revision);
        await reload();
      }
      onChanged();
    } catch (error) {
      setFailure(asApiError(error));
    } finally {
      setUploading(false);
    }
  };

  if (error !== null) return (
    <Notice tone="danger" title="读取技能失败"
      actions={<button type="button" className="btn-secondary" onClick={onBack}>返回目录</button>}>
      {error.message}
    </Notice>
  );
  if (detail === null) return <div className="loading">读取中…</div>;

  const editing = draft ?? { name: detail.name, description: detail.description, body: detail.body };
  const archived = detail.state === "archived";

  return (
    <section className="skill-pane" aria-labelledby={`skill-${skillId}`}>
      <div className="skill-pane-head">
        <button type="button" className="btn-secondary" onClick={onBack}>返回目录</button>
        <h3 id={`skill-${skillId}`}>{detail.name}</h3>
        <span className="skill-origin">{ORIGIN_LABELS[detail.origin] ?? detail.origin}</span>
        <label className="skill-managed">
          <input type="checkbox" checked={detail.managed} disabled={busy}
            onChange={(event) => void act(
              () => setSkillManaged(skillId, event.target.checked),
              event.target.checked ? "已允许复盘直接修改" : "已改为复盘需确认",
            )} />
          复盘可直接修改
        </label>
        <span className="skill-revision" title={detail.revision}>
          版本 {detail.revision.slice(0, 8)}
        </span>
      </div>

      {failure !== null && (
        <Notice tone="danger" title={conflict ? "技能已被更新" : "操作失败"}
          actions={conflict
            ? <button type="button" className="btn-secondary" onClick={() => void reload()}>重新载入（放弃本页修改）</button>
            : undefined}>
          {conflict
            ? "页面打开之后这个技能被改过。为避免覆盖，本次没有保存；重新载入后再改。"
            : failureText(failure)}
        </Notice>
      )}
      {note !== null && <div className="memory-note" role="status">{note}</div>}

      <label className="skill-field">
        <span>名称</span>
        <input className="input" value={editing.name} disabled={busy}
          onChange={(event) => setDraft({ ...editing, name: event.target.value })} />
      </label>
      <label className="skill-field">
        <span>一句话描述</span>
        <input className="input" value={editing.description} maxLength={160} disabled={busy}
          onChange={(event) => setDraft({ ...editing, description: event.target.value })} />
      </label>
      <label className="skill-field">
        <span>正文</span>
        <textarea className="input skill-body-input" rows={12} value={editing.body} disabled={busy}
          onChange={(event) => setDraft({ ...editing, body: event.target.value })} />
      </label>

      <div className="skill-pane-actions">
        {dirty && (
          <>
            <button type="button" className="btn-secondary" disabled={busy}
              onClick={() => setDraft(null)}>放弃修改</button>
            <button type="button" className="btn" disabled={busy} onClick={() => void save()}>
              {busy ? "保存中…" : "保存"}
            </button>
          </>
        )}
        <button type="button" className="btn-secondary" disabled={busy}
          onClick={() => void act(
            () => (archived ? restoreSkill(skillId) : archiveSkill(skillId)),
            archived ? "已恢复为启用" : "已归档，不再进入对话",
          )}>
          {archived ? "恢复启用" : "归档"}
        </button>
      </div>

      <div className="skill-attachments">
        <span className="skill-field-label">附件</span>
        {detail.files.length === 0 && <span className="skill-attachments-empty">无</span>}
        {detail.files.map((file) => (
          <span className="skill-attachment-chip" key={file.path}>
            <button type="button" className="skill-attachment-view" disabled={busy}
              onClick={async () => {
                try { setFileView({ path: file.path, content: (await readSkillFile(skillId, file.path)).content }); }
                catch (error) { setFailure(asApiError(error)); }
              }}>{file.path}</button>
            <button type="button" aria-label={`删除附件 ${file.path}`} disabled={busy}
              onClick={() => void act(() => removeSkillFile(skillId, file.path, detail.revision), "附件已删除")}>×</button>
          </span>
        ))}
        <input ref={fileInput} type="file" multiple hidden
          onChange={(event) => { void upload(event.target.files); event.target.value = ""; }} />
        <button type="button" className="btn-secondary" disabled={busy || uploading}
          onClick={() => fileInput.current?.click()}>
          {uploading ? "上传中…" : "上传附件"}
        </button>
      </div>
      {fileView !== null && (
        <div className="skill-file-view">
          <div className="skill-file-view-head">
            <span>{fileView.path}</span>
            <button type="button" onClick={() => setFileView(null)}>收起</button>
          </div>
          <pre>{fileView.content}</pre>
        </div>
      )}

      <div className="skill-versions">
        <span className="skill-field-label">历史版本</span>
        {versions === null && <span className="skill-attachments-empty">读取中…</span>}
        {versions !== null && versions.length === 0 && <span className="skill-attachments-empty">无</span>}
        {versions !== null && versions.map((version) => (
          <div className="skill-version-row" key={version.revision}>
            <span className="skill-revision" title={version.revision}>{version.revision.slice(0, 8)}</span>
            <span>{shortTime(version.created_at)}</span>
            <span className="skill-origin">{ORIGIN_LABELS[version.actor ?? ""] ?? version.actor}</span>
            <span className="skill-version-reason">{version.reason}</span>
            <button type="button" className="btn-secondary" disabled={busy}
              onClick={async () => {
                try { setViewing({ revision: version.revision, body: (await getSkillVersion(skillId, version.revision)).body }); }
                catch (error) { setFailure(asApiError(error)); }
              }}>查看</button>
            <button type="button" className="btn-secondary" disabled={busy || version.revision === detail.revision}
              onClick={() => void act(
                () => restoreSkillVersion(skillId, version.revision),
                `已恢复到 ${version.revision.slice(0, 8)}`,
              )}>恢复</button>
          </div>
        ))}
      </div>
      {viewing !== null && (
        <div className="skill-file-view">
          <div className="skill-file-view-head">
            <span>版本 {viewing.revision.slice(0, 8)} 的正文</span>
            <button type="button" onClick={() => setViewing(null)}>收起</button>
          </div>
          <pre>{viewing.body}</pre>
        </div>
      )}

      {detail.usage.length > 0 && (
        <div className="skill-usage-detail">
          <span className="skill-field-label">最近加载</span>
          <ul>
            {detail.usage.map((record) => (
              <li key={`${record.run_id}:${record.loaded_at}`}>
                <span>{shortTime(record.loaded_at)}</span>
                <span>{record.source === "manual" ? "手动装配" : "自动读取"}</span>
                <span className="skill-revision" title={record.revision}>{record.revision.slice(0, 8)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/** 待审变更：复盘对用户手写技能（managed=false）只能提出建议，批准后才落盘。 */
function ChangesPane({ changes, onChanged }: { changes: SkillChangeView[]; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ id: string; error: ApiError } | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const act = async (change: SkillChangeView, action: () => Promise<unknown>, message: string) => {
    if (busy !== null) return;
    setBusy(change.id);
    setFailure(null);
    setNote(null);
    try {
      await action();
      setNote(message);
      onChanged();
    } catch (error) {
      setFailure({ id: change.id, error: asApiError(error) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="skill-changes" aria-labelledby="skill-changes-title">
      <h3 id="skill-changes-title">待确认的技能变更</h3>
      {note !== null && <div className="memory-note" role="status">{note}</div>}
      {changes.map((change) => (
        <div className="skill-change" key={change.id}>
          <div className="skill-change-head">
            <strong>{change.skill_id ?? String(change.payload.skill_id ?? "")}</strong>
            <span className="skill-origin">{ORIGIN_LABELS[change.actor] ?? change.actor}</span>
            <span>{payloadPreview(change)}</span>
          </div>
          <div className="skill-change-reason">{change.reason}</div>
          {change.payload.body !== undefined && typeof change.payload.body === "string" && (
            <pre className="skill-change-body">{change.payload.body}</pre>
          )}
          {change.evidence_item_ids.length > 0 && (
            <div className="skill-change-evidence">依据条目：{change.evidence_item_ids.join("、")}</div>
          )}
          {failure?.id === change.id && (
            <div className="kb-dialog-error" role="alert">
              {failure.error.code === "skill_conflict"
                ? "技能在建议提出后被改过，版本已过期。"
                : failureText(failure.error)}
            </div>
          )}
          <div className="skill-pane-actions">
            <button type="button" className="btn-secondary" disabled={busy !== null}
              onClick={() => void act(change, () => rejectSkillChange(change.id), "已驳回")}>驳回</button>
            <button type="button" className="btn" disabled={busy !== null}
              onClick={() => void act(
                change,
                () => approveSkillChange(change.id, change.base_revision ?? ""),
                "已应用",
              )}>批准</button>
          </div>
        </div>
      ))}
    </section>
  );
}
