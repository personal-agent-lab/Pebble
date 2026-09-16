import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import {
  ApiError,
  createKbDocument,
  deleteKbDocument,
  getKbDocument,
  moveKbDocument,
  updateKbDocument,
  type KbDocument,
} from "../api";
import AppShell from "../components/AppShell";
import KbEditor from "../components/KbEditor";
import Notice from "../components/Notice";
import { documentLink, formatTags, parseTags, relativePath } from "../kb";
import { shortTime } from "../status";

const BACK_ICON = <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><polyline points="15 18 9 12 15 6" /></svg>;

type Mode = { kind: "view" } | { kind: "move"; target: string } | { kind: "delete" };

function asApiError(failure: unknown): ApiError {
  return failure instanceof ApiError ? failure : new ApiError("invalid_request", String(failure), 0);
}

function fieldMessage(error: ApiError): string {
  const detail = error.fieldErrors?.map((item) => item.message).join("；");
  return detail ? detail : error.message;
}

/**
 * 一份资料的阅读与编辑，也用于新建。
 *
 * 标题与标签是表单字段，正文是所见即所得编辑器；id 与时间只显示不可改。
 * 保存带上读取时的版本：资料在这期间被 Agent 或编辑器改过，保存会被拒绝，
 * 这里提示并让用户选择重新载入，不覆盖别人的改动。
 */
export default function KbDocumentPage({ creating = false }: { creating?: boolean }) {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const path = creating ? null : params.get("path");

  const [document, setDocument] = useState<KbDocument | null>(null);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [title, setTitle] = useState("");
  const [tags, setTags] = useState("");
  const [location, setLocation] = useState("");
  const [body, setBody] = useState("");
  const baseline = useRef<string | null>(creating ? "" : null);
  const reader = useRef<(() => string) | null>(null);
  const [touched, setTouched] = useState(false);
  const [editorKey, setEditorKey] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<ApiError | null>(null);
  const [mode, setMode] = useState<Mode>({ kind: "view" });
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (path === null) return;
    try {
      const loaded = await getKbDocument(path);
      setDocument(loaded);
      setTitle(loaded.title);
      setTags(formatTags(loaded.tags));
      setBody(loaded.body);
      baseline.current = null;
      setTouched(false);
      setEditorKey((key) => key + 1);
      setLoadError(null);
      setSaveError(null);
    } catch (failure) {
      setLoadError(asApiError(failure));
    }
  }, [path]);

  useEffect(() => { void load(); }, [load]);

  const bodyChanged = touched || (baseline.current !== null && body !== baseline.current);
  const dirty = creating
    ? title.trim() !== "" || body.trim() !== "" || touched
    : document !== null && (
      bodyChanged || title !== document.title || formatTags(parseTags(tags)) !== formatTags(document.tags)
    );

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const leave = (to: string) => {
    if (dirty && !window.confirm("有未保存的修改，确定离开吗？")) return;
    navigate(to);
  };

  const save = async () => {
    if (saving) return;
    setSaving(true);
    setSaveError(null);
    setNote(null);
    const latest = reader.current?.() ?? body;
    try {
      if (creating) {
        const created = await createKbDocument({
          title: title.trim(),
          body: latest,
          tags: parseTags(tags),
          ...(location.trim() ? { path: location.trim() } : {}),
        });
        setTouched(false);
        navigate(documentLink(created.path), { replace: true });
        return;
      }
      if (document === null) return;
      // 编辑器会重新排版原文：正文没有实际变化时提交原文，不让打开过就改写文件。
      const edited = baseline.current !== null && latest !== baseline.current;
      const nextTitle = title.trim();
      const nextTags = parseTags(tags);
      if (!edited && nextTitle === document.title && formatTags(nextTags) === formatTags(document.tags)) {
        setTouched(false);
        setBody(baseline.current ?? body);
        setNote("没有需要保存的改动");
        return;
      }
      const saved = await updateKbDocument(document.path, document.version, {
        title: nextTitle,
        body: edited ? latest : document.body,
        tags: nextTags,
      });
      await load();
      setNote(saved.index_status === "stale" ? "已保存，但当前不可检索" : "已保存");
    } catch (failure) {
      setSaveError(asApiError(failure));
    } finally {
      setSaving(false);
    }
  };

  const move = async (target: string) => {
    if (document === null || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      const moved = await moveKbDocument(document.path, document.version, target.trim());
      setMode({ kind: "view" });
      navigate(documentLink(moved.path), { replace: true });
      setNote(`已移动到 ${relativePath(moved.path)}`);
    } catch (failure) {
      setSaveError(asApiError(failure));
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (document === null || saving) return;
    setSaving(true);
    setSaveError(null);
    try {
      await deleteKbDocument(document.path, document.version);
      navigate("/kb", { replace: true });
    } catch (failure) {
      setSaveError(asApiError(failure));
      setMode({ kind: "view" });
    } finally {
      setSaving(false);
    }
  };

  if (!creating && (path === null || loadError !== null)) {
    return (
      <AppShell serviceError={loadError}>
        <div className="content">
          <Notice tone="danger" title={loadError?.httpStatus === 404 || path === null ? "资料不存在" : "读取资料失败"}
            actions={<button type="button" className="btn-secondary" onClick={() => navigate("/kb")}>返回资料列表</button>}>
            {loadError?.httpStatus === 404 || path === null
              ? "这份资料可能已被移动或删除。删除的资料可以在对话里让 Agent 找回。"
              : loadError?.message}
          </Notice>
        </div>
      </AppShell>
    );
  }

  const conflict = saveError?.code === "version_conflict";
  const ready = creating || document !== null;

  return (
    <AppShell>
      <div className="chat-top">
        <button type="button" className="back" onClick={() => leave("/kb")} aria-label="返回资料列表">{BACK_ICON}</button>
        <div style={{ minWidth: 0, flex: 1 }}>
          <h2>{creating ? "新建资料" : (document?.title ?? "读取中…")}</h2>
          <div className="sub">
            {document !== null && relativePath(document.path)}
            {creating && "保存后写入资料库，并留下历史版本"}
          </div>
        </div>
        {note !== null && !dirty && <span className="kb-note" role="status">{note}</span>}
        <button type="button" className="btn" disabled={!ready || !dirty || saving || (creating && title.trim() === "")}
          onClick={() => void save()}>
          {saving ? "保存中…" : "保存"}
        </button>
      </div>

      <div className="content kb-doc">
        {saveError !== null && (
          <Notice tone="danger" title={conflict ? "资料已被修改" : "操作失败"}
            actions={conflict
              ? <button type="button" className="btn-secondary" onClick={() => void load()}>重新载入（放弃本页修改）</button>
              : undefined}>
            {conflict
              ? "这份资料在你打开之后被修改过（可能来自 Agent 或文件编辑器）。为避免覆盖，本次没有保存。"
              : fieldMessage(saveError)}
          </Notice>
        )}

        {ready && (
          <>
            <input
              className="kb-title"
              value={title}
              placeholder="资料标题"
              aria-label="资料标题"
              onChange={(event) => setTitle(event.target.value)}
            />
            <div className="kb-fields">
              <label className="kb-field">
                <span>标签</span>
                <input className="input" value={tags} placeholder="用逗号分隔，例如：课程, GSE"
                  onChange={(event) => setTags(event.target.value)} />
              </label>
              {creating && (
                <label className="kb-field">
                  <span>位置</span>
                  <input className="input" value={location} placeholder="可选，例如：课程/gse-lab1（省略时放入收件目录）"
                    onChange={(event) => setLocation(event.target.value)} />
                </label>
              )}
            </div>

            <KbEditor
              key={`${document?.version ?? "new"}:${editorKey}`}
              label="资料正文"
              initial={creating ? "" : (document?.body ?? "")}
              onReady={(markdown) => {
                baseline.current = markdown;
                setBody(markdown);
              }}
              onChange={setBody}
              onInput={() => setTouched(true)}
              reader={reader}
            />

            {document !== null && (
              <div className="kb-meta">
                <span>创建 {document.created_at ? shortTime(document.created_at) : "—"}</span>
                <span>更新 {document.updated_at ? shortTime(document.updated_at) : "—"}</span>
                <span className="kb-meta-id">{document.id}</span>
              </div>
            )}

            {document !== null && (
              <div className="kb-actions">
                {mode.kind === "view" && (
                  <>
                    <button type="button" className="btn-secondary" disabled={dirty}
                      title={dirty ? "先保存或放弃修改" : undefined}
                      onClick={() => setMode({ kind: "move", target: relativePath(document.path) })}>
                      移动或重命名
                    </button>
                    <button type="button" className="btn-secondary danger" disabled={dirty}
                      title={dirty ? "先保存或放弃修改" : undefined}
                      onClick={() => setMode({ kind: "delete" })}>
                      删除
                    </button>
                  </>
                )}
                {mode.kind === "move" && (
                  <form className="kb-inline" onSubmit={(event) => { event.preventDefault(); void move(mode.target); }}>
                    <input className="input" value={mode.target} aria-label="新位置" autoFocus
                      onChange={(event) => setMode({ kind: "move", target: event.target.value })} />
                    <button type="submit" className="btn" disabled={saving || mode.target.trim() === ""}>移动</button>
                    <button type="button" className="btn-secondary" onClick={() => setMode({ kind: "view" })}>取消</button>
                  </form>
                )}
                {mode.kind === "delete" && (
                  <div className="kb-inline">
                    <span className="kb-inline-text">删除后资料库里不再有这份资料，历史版本仍保留，可在对话里让 Agent 找回。</span>
                    <button type="button" className="btn danger" disabled={saving} onClick={() => void remove()}>确认删除</button>
                    <button type="button" className="btn-secondary" onClick={() => setMode({ kind: "view" })}>取消</button>
                  </div>
                )}
              </div>
            )}
          </>
        )}

        {!ready && <div className="loading">读取中…</div>}
      </div>
    </AppShell>
  );
}
