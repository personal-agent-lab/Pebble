import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import {
  ApiError,
  createKbFolder,
  listKbDocuments,
  listKbFolders,
  searchKb,
  type KbHit,
  type KbListItem,
} from "../api";
import AppShell from "../components/AppShell";
import {
  DOC_ICON,
  DeleteDialog,
  FOLDER_ICON,
  KbItemMenu,
  MoveDialog,
  RenameDialog,
  type KbAction,
} from "../components/KbDialogs";
import Notice from "../components/Notice";
import {
  breadcrumbs,
  countIn,
  displayTitle,
  documentLink,
  folderExists,
  folderLink,
  folderView,
  newDocumentLink,
  normalizeDir,
  parentDir,
  type KbFolderEntry,
} from "../kb";
import { shortTime } from "../status";

const SEARCH_DELAY_MS = 300;

const BACK_ICON = <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><polyline points="15 18 9 12 15 6" /></svg>;

const CHEVRON_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="6 9 12 15 18 9" />
  </svg>
);

function asApiError(failure: unknown): ApiError {
  return failure instanceof ApiError ? failure : new ApiError("invalid_request", String(failure), 0);
}

/** 位置只到文件夹：文件名由程序按标题生成，界面上不展示。 */
function folderLabel(path: string): string {
  const dir = parentDir(path);
  return dir ? `资料库 / ${dir.split("/").join(" / ")}` : "资料库";
}

// 文件夹行没有菜单，留同宽的空位，时间列与资料行上下对齐。
function FolderRow({ folder }: { folder: KbFolderEntry }) {
  return (
    <div className="kb-row">
      <Link className="kb-row-link" to={folderLink(folder.path)}>
        {FOLDER_ICON}
        <span className="kb-row-title">{folder.name}</span>
        <span className="kb-row-summary">{folder.count > 0 ? `${folder.count} 份资料` : "空文件夹"}</span>
        <span className="kb-row-time">{folder.updated_at ? shortTime(folder.updated_at) : "—"}</span>
      </Link>
      <span className="kb-row-slot" />
    </div>
  );
}

function DocumentRow({ document, onAction }: { document: KbListItem; onAction: (action: KbAction) => void }) {
  const target = { path: document.path, version: document.version, title: displayTitle(document) };
  return (
    <div className="kb-row">
      <Link className="kb-row-link" to={documentLink(document.path)}>
        {DOC_ICON}
        <span className="kb-row-title">{displayTitle(document)}</span>
        {document.summary && <span className="kb-row-summary">{document.summary}</span>}
        <span className="kb-row-time">{document.updated_at ? shortTime(document.updated_at) : ""}</span>
      </Link>
      <KbItemMenu
        onRename={() => onAction({ kind: "rename", target })}
        onMove={() => onAction({ kind: "move", target })}
        onDelete={() => onAction({ kind: "delete", target })}
      />
    </div>
  );
}

function SearchResults({ hits }: { hits: KbHit[] }) {
  if (hits.length === 0) return <div className="empty"><div className="empty-sub">没有找到相关资料</div></div>;
  return (
    <div className="kb-hits">
      {hits.map((hit) => (
        <Link className="kb-hit" key={`${hit.path}:${hit.heading}`} to={documentLink(hit.path)}>
          <div className="kb-hit-title">{hit.heading || displayTitle(hit)}</div>
          <div className="kb-hit-snippet">{hit.snippet}</div>
          <div className="kb-hit-path">
            {folderLabel(hit.path)}{hit.heading ? ` / ${displayTitle(hit)}` : ""}
          </div>
        </Link>
      ))}
    </div>
  );
}

/** “新建”下拉菜单：在当前文件夹里新建 Markdown 文档或文件夹。 */
function NewMenu({ onDocument, onFolder }: { onDocument: () => void; onFolder: () => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (root.current !== null && !root.current.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const pick = (action: () => void) => {
    setOpen(false);
    action();
  };

  return (
    <div className="kb-new" ref={root}>
      <button type="button" className="btn kb-new-button" aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen((value) => !value)}>
        新建{CHEVRON_ICON}
      </button>
      {open && (
        <div className="task-menu kb-new-menu" role="menu">
          <button type="button" role="menuitem" className="task-menu-item" onClick={() => pick(onDocument)}>
            {DOC_ICON}Markdown 文档
          </button>
          <button type="button" role="menuitem" className="task-menu-item" onClick={() => pick(onFolder)}>
            {FOLDER_ICON}文件夹
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * 新建文件夹的对话框：建在当前文件夹下。
 * Esc、点击遮罩或“取消”关闭；名称为空时“创建”不可用，失败原因留在对话框里。
 */
function NewFolderDialog({ dir, onCreated, onClose }: { dir: string; onCreated: () => void; onClose: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const submit = async () => {
    const trimmed = name.trim();
    if (busy || trimmed === "") return;
    setBusy(true);
    setFailure(null);
    try {
      await createKbFolder(dir ? `${dir}/${trimmed}` : trimmed);
      onCreated();
    } catch (error) {
      const apiError = asApiError(error);
      setFailure(apiError.fieldErrors?.map((item) => item.message).join("；") || apiError.message);
      setBusy(false);
    }
  };

  return (
    <div className="kb-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <form className="kb-dialog" role="dialog" aria-modal="true" aria-labelledby="kb-new-folder-title"
        onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <h3 id="kb-new-folder-title" className="kb-dialog-title">新建文件夹</h3>
        {dir && <div className="kb-dialog-sub">创建在：{dir}</div>}
        <label className="kb-dialog-field">
          <span>文件夹名称</span>
          <input className="input" value={name} autoFocus
            onChange={(event) => { setName(event.target.value); setFailure(null); }} />
        </label>
        {failure !== null && <div className="kb-dialog-error" role="alert">{failure}</div>}
        <div className="kb-dialog-actions">
          <button type="button" className="btn-secondary" onClick={onClose}>取消</button>
          <button type="submit" className="btn" disabled={busy || name.trim() === ""}>创建</button>
        </div>
      </form>
    </div>
  );
}

/**
 * 资料浏览与搜索，按文件夹逐层浏览。
 *
 * 当前文件夹与搜索词都写在地址里，从资料页返回时还停在原处；
 * 搜索范围是整个资料库，不受当前文件夹限制。
 */
export default function KbPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const dir = normalizeDir(params.get("dir"));
  const [text, setText] = useState(q);
  const [documents, setDocuments] = useState<KbListItem[] | null>(null);
  const [folders, setFolders] = useState<string[]>([]);
  const [hits, setHits] = useState<KbHit[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [action, setAction] = useState<KbAction | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [listed, folderList] = await Promise.all([listKbDocuments(), listKbFolders()]);
      setDocuments(listed.documents);
      setFolders(folderList.folders);
      setError(null);
    } catch (failure) {
      setError(asApiError(failure));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  const closeFolderDialog = useCallback(() => setCreatingFolder(false), []);
  const closeAction = () => setAction(null);
  const finish = (message: string | null = null) => {
    setAction(null);
    setNote(message);
    void load();
  };
  // 换文件夹时收起新建表单；搜索框跟随地址，避免旧搜索词在新文件夹里被重新提交。
  useEffect(() => {
    setCreatingFolder(false);
    setNote(null);
    setText(params.get("q") ?? "");
  }, [dir]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const trimmed = text.trim();
    const timer = window.setTimeout(() => {
      if (trimmed === q) return;
      setParams({ ...(dir ? { dir } : {}), ...(trimmed ? { q: trimmed } : {}) }, { replace: true });
    }, SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [text, q, dir, setParams]);

  useEffect(() => {
    if (q === "") {
      setHits(null);
      return;
    }
    let cancelled = false;
    searchKb(q)
      .then((result) => { if (!cancelled) { setHits(result.results); setError(null); } })
      .catch((failure) => { if (!cancelled) setError(asApiError(failure)); });
    return () => { cancelled = true; };
  }, [q]);

  const view = documents === null ? null : folderView(documents, folders, dir);
  const exists = documents === null || folderExists(documents, folders, dir);
  const crumbs = breadcrumbs(dir);
  const current = crumbs.length > 0 ? crumbs[crumbs.length - 1] : undefined;
  const parents = crumbs.slice(0, -1);

  return (
    <AppShell serviceError={error}>
      <div className="topbar">
        {current !== undefined && (
          <Link className="back" to={folderLink(parents.length > 0 ? parents[parents.length - 1].path : "")} aria-label="返回上一级">
            {BACK_ICON}
          </Link>
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          {/* 子文件夹里标题就是当前文件夹，面包屑只列上级路径，不重复当前名。 */}
          <h2 className="kb-title">{current !== undefined ? <>{FOLDER_ICON}{current.name}</> : "资料"}</h2>
          <nav className="kb-crumbs" aria-label="当前位置">
            {current !== undefined && (
              <span>
                <Link to={folderLink("")}>资料库</Link>
                {parents.map((crumb) => (
                  <span key={crumb.path}>
                    <span className="kb-crumb-sep">/</span>
                    <Link to={folderLink(crumb.path)}>{crumb.name}</Link>
                  </span>
                ))}
              </span>
            )}
            {documents !== null && (
              <span className="kb-crumb-count">{countIn(documents, dir)} 份资料</span>
            )}
          </nav>
        </div>
        {exists && (
          <NewMenu onDocument={() => navigate(newDocumentLink(dir))} onFolder={() => setCreatingFolder(true)} />
        )}
      </div>
      <div className="content kb-content">
        <input
          className="input kb-search"
          type="search"
          value={text}
          placeholder="搜索资料内容、标题或说明…"
          aria-label="搜索资料"
          onChange={(event) => setText(event.target.value)}
        />

        {note !== null && <div className="kb-list-note" role="status">{note}</div>}

        {error !== null && (
          <Notice tone="danger" title={error.unavailable ? "资料库未接入" : "读取资料失败"}
            actions={<button type="button" className="btn-secondary" onClick={() => void load()}>重试</button>}>
            {error.message}
          </Notice>
        )}

        {q !== "" && hits !== null && <SearchResults hits={hits} />}

        {q === "" && view !== null && !exists && (
          <Notice tone="danger" title="文件夹不存在"
            actions={<button type="button" className="btn-secondary" onClick={() => navigate(folderLink(""))}>回到资料库</button>}>
            这个文件夹可能已被移动或删除。
          </Notice>
        )}

        {q === "" && view !== null && exists && (
          view.folders.length === 0 && view.documents.length === 0
            ? <div className="empty">
                <div className="empty-title">{dir ? "这个文件夹还是空的" : "还没有资料"}</div>
                <div className="empty-sub">新建一份，或在对话里让 Agent 帮你保存。</div>
              </div>
            : <div className="kb-list">
                <div className="kb-list-head" aria-hidden="true">
                  <span>名称</span>
                  <span className="kb-row-time">最近更新</span>
                  <span className="kb-row-slot" />
                </div>
                {view.folders.map((folder) => <FolderRow key={folder.path} folder={folder} />)}
                {view.documents.map((document) => (
                  <DocumentRow key={document.path} document={document} onAction={(next) => { setNote(null); setAction(next); }} />
                ))}
              </div>
        )}

        {documents === null && error === null && <div className="loading">读取中…</div>}
      </div>

      {creatingFolder && (
        <NewFolderDialog dir={dir}
          onCreated={() => { setCreatingFolder(false); void load(); }}
          onClose={closeFolderDialog} />
      )}
      {action?.kind === "rename" && (
        <RenameDialog target={action.target} onClose={closeAction}
          onDone={() => finish()} />
      )}
      {action?.kind === "move" && (
        <MoveDialog target={action.target} onClose={closeAction}
          onDone={() => finish()} />
      )}
      {action?.kind === "delete" && (
        <DeleteDialog target={action.target} onClose={closeAction}
          onDone={() => finish(`已删除「${action.target.title}」`)} />
      )}
    </AppShell>
  );
}
