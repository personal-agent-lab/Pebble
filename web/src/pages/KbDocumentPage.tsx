import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import {
  ApiError,
  createKbDocument,
  describeKbAsset,
  draftKbSummary,
  getKbDocument,
  updateKbDocument,
  uploadKbAsset,
  type KbDocument,
} from "../api";
import AppShell from "../components/AppShell";
import { DeleteDialog, KbItemMenu, MoveDialog, RenameDialog, type KbAction } from "../components/KbDialogs";
import KbEditor from "../components/KbEditor";
import Notice from "../components/Notice";
import { breadcrumbs, documentLink, folderLink, normalizeDir, parentDir } from "../kb";
import { shortTime } from "../status";

const SPARKLE_ICON = <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" /><path d="M19 16l.7 1.8 1.8.7-1.8.7L19 21l-.7-1.8-1.8-.7 1.8-.7z" /></svg>;
const BACK_ICON = <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><polyline points="15 18 9 12 15 6" /></svg>;

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
 * 标题与一句话说明是正文上方的可编辑行，正文是所见即所得编辑器；id 与时间只显示不可改。
 * 保存带上读取时的版本：资料在这期间被 Agent 或编辑器改过，保存会被拒绝，
 * 这里提示并让用户选择重新载入，不覆盖别人的改动。
 */
export default function KbDocumentPage({ creating = false }: { creating?: boolean }) {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const path = creating ? null : params.get("path");
  // 新建时建在进入新建页时所在的文件夹；已有资料返回它所在的文件夹。
  const directory = creating ? normalizeDir(params.get("dir")) : null;
  const backTo = folderLink(directory ?? (path === null ? "" : parentDir(path)));

  const [document, setDocument] = useState<KbDocument | null>(null);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [title, setTitle] = useState("");
  const [summary, setSummary] = useState("");
  const [body, setBody] = useState("");
  const baseline = useRef<string | null>(creating ? "" : null);
  const reader = useRef<(() => string) | null>(null);
  const [touched, setTouched] = useState(false);
  // 正文是否为空：变更通知有防抖，输入时直接读编辑器，按钮状态不滞后。
  const [bodyBlank, setBodyBlank] = useState(creating);
  const [editorKey, setEditorKey] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<ApiError | null>(null);
  const [action, setAction] = useState<KbAction | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (path === null) return;
    try {
      const loaded = await getKbDocument(path);
      setDocument(loaded);
      setTitle(loaded.title);
      setSummary(loaded.summary);
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
      bodyChanged || title !== document.title || summary.trim() !== document.summary
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

  // 点保存按钮存完回到上一页（资料所在文件夹）；⌘S 存完留在原页继续编辑。
  const save = async (exit = false) => {
    const latest = reader.current?.() ?? body;
    if (saving || title.trim() === "" || latest.trim() === "") return;
    setSaving(true);
    setSaveError(null);
    setNote(null);
    try {
      if (creating) {
        const created = await createKbDocument({
          title: title.trim(),
          summary: summary.trim(),
          body: latest,
          directory: directory ?? "",
        });
        setTouched(false);
        navigate(exit ? backTo : documentLink(created.path), { replace: true });
        return;
      }
      if (document === null) return;
      // 编辑器会重新排版原文：正文没有实际变化时提交原文，不让打开过就改写文件。
      const edited = baseline.current !== null && latest !== baseline.current;
      const nextTitle = title.trim();
      const nextSummary = summary.trim();
      if (!edited && nextTitle === document.title && nextSummary === document.summary) {
        setTouched(false);
        setBody(baseline.current ?? body);
        if (exit) {
          navigate(backTo);
          return;
        }
        setNote("没有需要保存的改动");
        return;
      }
      const saved = await updateKbDocument(document.path, document.version, {
        title: nextTitle,
        summary: nextSummary,
        body: edited ? latest : document.body,
      });
      // 已存但暂不可检索时留在本页把提示给用户看到，不直接离开。
      if (exit && saved.index_status !== "stale") {
        setTouched(false);
        navigate(backTo);
        return;
      }
      const message = saved.index_status === "stale" ? "已保存，但当前不可检索" : "已保存";
      // 改了标题时文件名随之更新：换到新地址，由地址变化重新载入。
      if (saved.path !== document.path) {
        setTouched(false);
        navigate(documentLink(saved.path), { replace: true });
      } else {
        await load();
      }
      setNote(message);
    } catch (failure) {
      setSaveError(asApiError(failure));
    } finally {
      setSaving(false);
    }
  };

  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void saveRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 说明由轻量模型按当前标题与正文起草，填进输入框后仍由用户确认、随资料一起保存。
  const draftSummary = async () => {
    const latest = reader.current?.() ?? body;
    if (drafting || latest.trim() === "") return;
    setDrafting(true);
    setDraftError(null);
    try {
      const drafted = await draftKbSummary(title.trim(), latest);
      setSummary(drafted.summary);
    } catch (failure) {
      setDraftError(fieldMessage(asApiError(failure)));
    } finally {
      setDrafting(false);
    }
  };

  // 重命名与移动都可能换地址；地址不变时直接重新载入。
  const relocated = (nextPath: string, message: string) => {
    setAction(null);
    setNote(message);
    if (document !== null && nextPath === document.path) void load();
    else navigate(documentLink(nextPath), { replace: true });
  };

  if (!creating && (path === null || loadError !== null)) {
    return (
      <AppShell serviceError={loadError}>
        <div className="content">
          <Notice tone="danger" title={loadError?.httpStatus === 404 || path === null ? "资料不存在" : "读取资料失败"}
            actions={<button type="button" className="btn-secondary" onClick={() => navigate(backTo)}>返回资料列表</button>}>
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
      <div className="chat-top kb-doc-top">
        <button type="button" className="back" onClick={() => leave(backTo)} aria-label="返回资料列表">{BACK_ICON}</button>
        {/* 标题只在正文区出现一次：顶栏只交代这份资料在哪个文件夹、保存状态如何；
            文件名由程序按标题生成，不展示。 */}
        <nav className="kb-doc-where" aria-label="所在位置">
          {creating && <span>保存到：</span>}
          <Link to={folderLink("")}>资料库</Link>
          {breadcrumbs(directory ?? (document !== null ? parentDir(document.path) : "")).map((crumb) => (
            <span key={crumb.path}>
              <span className="kb-crumb-sep">/</span>
              <Link to={folderLink(crumb.path)}>{crumb.name}</Link>
            </span>
          ))}
        </nav>
        {dirty && <span className="kb-dirty">未保存</span>}
        {note !== null && !dirty && <span className="kb-note" role="status">{note}</span>}
        <button type="button" className={dirty ? "btn" : "btn-secondary"} disabled={!ready || !dirty || saving || title.trim() === "" || bodyBlank}
          title={ready && dirty && (title.trim() === "" || bodyBlank) ? "标题和正文都不能为空" : "保存（⌘S）"}
          onClick={() => void save(true)}>
          {saving ? "保存中…" : "保存"}
        </button>
        {document !== null && (
          <KbItemMenu
            disabledReason={dirty ? "先保存或放弃修改" : undefined}
            onRename={() => setAction({ kind: "rename", target: document })}
            onMove={() => setAction({ kind: "move", target: document })}
            onDelete={() => setAction({ kind: "delete", target: document })}
          />
        )}
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
            {/* 说明是标题下的一行副标题，不做成表单行，读的时候不抢正文。 */}
            <div className="kb-summary">
              <input className="kb-summary-input" value={summary} maxLength={120} aria-label="说明"
                placeholder="摘要"
                onChange={(event) => setSummary(event.target.value)} />
              <button type="button" className={`kb-summary-draft${drafting ? " drafting" : ""}`}
                disabled={drafting || bodyBlank}
                aria-label="生成说明"
                title={bodyBlank ? "先写正文再生成说明" : "按标题和正文生成一句话说明"}
                onClick={() => void draftSummary()}>
                {SPARKLE_ICON}<span>{drafting ? "生成中…" : "生成"}</span>
              </button>
            </div>
            {draftError !== null && <p className="kb-field-error" role="alert">{draftError}</p>}

            <KbEditor
              key={`${document?.version ?? "new"}:${editorKey}`}
              label="资料正文"
              initial={creating ? "" : (document?.body ?? "")}
              onReady={(markdown) => {
                baseline.current = markdown;
                setBody(markdown);
                setBodyBlank(markdown.trim() === "");
              }}
              onChange={(markdown) => {
                setBody(markdown);
                setBodyBlank(markdown.trim() === "");
              }}
              onInput={() => {
                setTouched(true);
                const latest = reader.current?.();
                if (latest !== undefined) setBodyBlank(latest.trim() === "");
              }}
              reader={reader}
              uploadImage={uploadKbAsset}
              describeImage={describeKbAsset}
            />

            {document !== null && (
              <div className="kb-meta">
                <span>创建 {document.created_at ? shortTime(document.created_at) : "—"}</span>
                <span>更新 {document.updated_at ? shortTime(document.updated_at) : "—"}</span>
                <span className="kb-meta-id">{document.id}</span>
              </div>
            )}

          </>
        )}

        {!ready && <div className="loading">读取中…</div>}
      </div>

      {document !== null && action?.kind === "rename" && (
        <RenameDialog target={action.target} onClose={() => setAction(null)}
          onDone={(result) => relocated(result.path, "已重命名")} />
      )}
      {document !== null && action?.kind === "move" && (
        <MoveDialog target={action.target} onClose={() => setAction(null)}
          onDone={(result) => relocated(result.path, "已移动")} />
      )}
      {document !== null && action?.kind === "delete" && (
        <DeleteDialog target={action.target} onClose={() => setAction(null)}
          onDone={() => navigate(backTo, { replace: true })} />
      )}
    </AppShell>
  );
}
