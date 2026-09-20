import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import {
  ApiError,
  createKbFolder,
  deleteKbDocument,
  listKbDocuments,
  listKbFolders,
  moveKbDocument,
  updateKbDocument,
  type KbListItem,
  type KbWriteResult,
} from "../api";
import { breadcrumbs, displayTitle, fileName, folderView, parentDir } from "../kb";

export const FOLDER_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
  </svg>
);

export const DOC_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
    <polyline points="14 3 14 8 19 8" />
  </svg>
);

const MORE_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="currentColor">
    <circle cx="5" cy="12" r="1.7" />
    <circle cx="12" cy="12" r="1.7" />
    <circle cx="19" cy="12" r="1.7" />
  </svg>
);

const RENAME_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 20h4L19 9l-4-4L4 16z" /><path d="M13.5 6.5l4 4" />
  </svg>
);

const MOVE_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v2" /><path d="M3 7v10a2 2 0 0 0 2 2h7" />
    <path d="M15 17h6" /><polyline points="18 14 21 17 18 20" />
  </svg>
);

const DELETE_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 7h16" /><path d="M10 11v6M14 11v6" /><path d="M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12" /><path d="M9 7V4h6v3" />
  </svg>
);

const CHEVRON_RIGHT = <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 6 15 12 9 18" /></svg>;

/** 对话框要操作的那份资料：路径与读取时的版本，版本对不上时后端拒绝。 */
export type KbTarget = { path: string; version: string; title: string };

function failureText(failure: unknown): string {
  if (!(failure instanceof ApiError)) return String(failure);
  if (failure.code === "version_conflict") return "这份资料在你打开之后被修改过，请刷新后再试。";
  return failure.fieldErrors?.map((item) => item.message).join("；") || failure.message;
}

/**
 * 资料的“⋯”菜单：重命名、移动、删除，具体操作交给对话框。
 * 弹层用 fixed 定位，坐标在打开时按按钮量一次，不被列表或页面的滚动区裁掉。
 */
export function KbItemMenu({ disabledReason, onRename, onMove, onDelete }: {
  disabledReason?: string;
  onRename: () => void;
  onMove: () => void;
  onDelete: () => void;
}) {
  const [anchor, setAnchor] = useState<{ top: number; right: number } | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const open = anchor !== null;

  useEffect(() => {
    if (!open) return;
    const close = () => setAnchor(null);
    const onPointerDown = (event: MouseEvent) => {
      if (root.current !== null && !root.current.contains(event.target as Node)) close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const toggle = () => {
    if (open) {
      setAnchor(null);
      return;
    }
    const rect = button.current?.getBoundingClientRect();
    if (rect === undefined) return;
    setAnchor({ top: rect.bottom + 4, right: document.documentElement.clientWidth - rect.right });
  };

  const pick = (action: () => void) => {
    setAnchor(null);
    action();
  };

  const disabled = disabledReason !== undefined;
  return (
    <div className={`kb-more${open ? " open" : ""}`} ref={root}>
      <button type="button" ref={button} className="kb-more-button" aria-label="更多操作"
        aria-haspopup="menu" aria-expanded={open} onClick={toggle}>
        {MORE_ICON}
      </button>
      {anchor !== null && (
        <div className="task-menu kb-more-menu" role="menu" title={disabledReason}
          style={{ position: "fixed", top: anchor.top, right: anchor.right }}>
          <button type="button" role="menuitem" className="task-menu-item" disabled={disabled} onClick={() => pick(onRename)}>
            {RENAME_ICON}重命名
          </button>
          <button type="button" role="menuitem" className="task-menu-item" disabled={disabled} onClick={() => pick(onMove)}>
            {MOVE_ICON}移动
          </button>
          <button type="button" role="menuitem" className="task-menu-item danger" disabled={disabled} onClick={() => pick(onDelete)}>
            {DELETE_ICON}删除
          </button>
          {disabled && <div className="kb-more-hint">{disabledReason}</div>}
        </div>
      )}
    </div>
  );
}

/** 对话框外壳：Esc、点遮罩关闭；忙的时候不响应关闭，避免操作结果无处可报。 */
export function KbDialog({ labelledBy, busy = false, wide = false, onClose, onSubmit, children }: {
  labelledBy: string;
  busy?: boolean;
  wide?: boolean;
  onClose: () => void;
  onSubmit: () => void;
  children: ReactNode;
}) {
  const closeRef = useRef(onClose);
  closeRef.current = busy ? () => {} : onClose;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    onSubmit();
  };

  return (
    <div className="kb-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) closeRef.current(); }}>
      <form className={`kb-dialog${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby={labelledBy} onSubmit={submit}>
        {children}
      </form>
    </div>
  );
}

/**
 * 重命名就是改标题：用户在列表与对话里认的都是标题，文件名由后端随标题更新。
 */
export function RenameDialog({ target, onDone, onClose }: {
  target: KbTarget;
  onDone: (result: KbWriteResult) => void;
  onClose: () => void;
}) {
  const [title, setTitle] = useState(target.title);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { input.current?.select(); }, []);

  const trimmed = title.trim();
  const submit = async () => {
    if (busy || trimmed === "" || trimmed === target.title) return;
    setBusy(true);
    setFailure(null);
    try {
      onDone(await updateKbDocument(target.path, target.version, { title: trimmed }));
    } catch (error) {
      setFailure(failureText(error));
      setBusy(false);
    }
  };

  return (
    <KbDialog labelledBy="kb-rename-title" busy={busy} onClose={onClose} onSubmit={() => void submit()}>
      <h3 id="kb-rename-title" className="kb-dialog-title">重命名资料</h3>
      <input ref={input} className="input" value={title} aria-label="资料名称" autoFocus
        onChange={(event) => { setTitle(event.target.value); setFailure(null); }} />
      {failure !== null && <div className="kb-dialog-error" role="alert">{failure}</div>}
      <div className="kb-dialog-actions">
        <button type="button" className="btn-secondary" disabled={busy} onClick={onClose}>取消</button>
        <button type="submit" className="btn" disabled={busy || trimmed === "" || trimmed === target.title}>
          {busy ? "重命名中…" : "重命名"}
        </button>
      </div>
    </KbDialog>
  );
}

/** 目标文件夹里不和已有资料重名的文件名：同名时依次编号，与后端新建资料的规则一致。 */
export function freeName(name: string, taken: string[]): string {
  const used = new Set(taken.map((item) => item.toLowerCase()));
  const stem = name.replace(/\.md$/, "");
  for (let number = 1; ; number += 1) {
    const candidate = number === 1 ? `${stem}.md` : `${stem} ${number}.md`;
    if (!used.has(candidate.toLowerCase())) return candidate;
  }
}

/**
 * “移动到…”：像文件管理器一样逐层进入文件夹，在要放的那一层点“移到这里”。
 * 同层资料只灰着列出，让人看清这里已有什么；文件名沿用原名，重名时自动编号。
 */
export function MoveDialog({ target, onDone, onClose }: {
  target: KbTarget;
  onDone: (result: KbWriteResult & { previous_path: string }) => void;
  onClose: () => void;
}) {
  const origin = parentDir(target.path);
  const [dir, setDir] = useState(origin);
  const [documents, setDocuments] = useState<KbListItem[] | null>(null);
  const [folders, setFolders] = useState<string[]>([]);
  const [naming, setNaming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const load = async () => {
    const [listed, folderList] = await Promise.all([listKbDocuments(), listKbFolders()]);
    setDocuments(listed.documents);
    setFolders(folderList.folders);
  };

  useEffect(() => {
    load().catch((error) => setFailure(failureText(error)));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const view = documents === null ? null : folderView(documents, folders, dir);
  const crumbs = breadcrumbs(dir);

  const enter = (next: string) => {
    setDir(next);
    setNaming(null);
    setFailure(null);
  };

  const createFolder = async () => {
    const name = (naming ?? "").trim();
    if (busy || name === "") return;
    setBusy(true);
    setFailure(null);
    try {
      const created = await createKbFolder(dir ? `${dir}/${name}` : name);
      await load();
      enter(created.path.replace(/^kb\//, ""));
    } catch (error) {
      setFailure(failureText(error));
    } finally {
      setBusy(false);
    }
  };

  const move = async () => {
    if (naming !== null) {
      await createFolder();
      return;
    }
    if (busy || view === null || dir === origin) return;
    setBusy(true);
    setFailure(null);
    const name = freeName(fileName(target.path), view.documents.map((item) => fileName(item.path)));
    try {
      onDone(await moveKbDocument(target.path, target.version, dir ? `${dir}/${name}` : name));
    } catch (error) {
      setFailure(failureText(error));
      setBusy(false);
    }
  };

  return (
    <KbDialog labelledBy="kb-move-title" busy={busy} wide onClose={onClose} onSubmit={() => void move()}>
      <h3 id="kb-move-title" className="kb-dialog-title">移动到…</h3>
      <nav className="kb-move-crumbs" aria-label="目标位置">
        <button type="button" className="kb-move-crumb" disabled={dir === ""} onClick={() => enter("")}>资料库</button>
        {crumbs.map((crumb) => (
          <span key={crumb.path}>
            <span className="kb-crumb-sep">/</span>
            <button type="button" className="kb-move-crumb" disabled={crumb.path === dir} onClick={() => enter(crumb.path)}>
              {crumb.name}
            </button>
          </span>
        ))}
      </nav>
      <div className="kb-move-list">
        {naming !== null && (
          <div className="kb-move-row kb-move-new">
            {FOLDER_ICON}
            <input className="input" value={naming} placeholder="新文件夹名称" aria-label="新文件夹名称" autoFocus
              onChange={(event) => { setNaming(event.target.value); setFailure(null); }}
              onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); setNaming(null); } }} />
            <button type="button" className="btn" disabled={busy || naming.trim() === ""} onClick={() => void createFolder()}>创建</button>
          </div>
        )}
        {view === null && failure === null && <div className="kb-move-empty">读取中…</div>}
        {view !== null && view.folders.map((folder) => (
          <button type="button" key={folder.path} className="kb-move-row kb-move-folder" onClick={() => enter(folder.path)}>
            {FOLDER_ICON}<span className="kb-move-name">{folder.name}</span>{CHEVRON_RIGHT}
          </button>
        ))}
        {view !== null && view.documents.map((item) => (
          <div key={item.path} className="kb-move-row kb-move-doc" aria-disabled="true">
            {DOC_ICON}<span className="kb-move-name">{displayTitle(item)}</span>
            {item.path === target.path && <span className="kb-move-here">当前位置</span>}
          </div>
        ))}
        {view !== null && view.folders.length === 0 && view.documents.length === 0 && naming === null && (
          <div className="kb-move-empty">这个文件夹是空的</div>
        )}
      </div>
      {failure !== null && <div className="kb-dialog-error" role="alert">{failure}</div>}
      <div className="kb-dialog-actions">
        <button type="button" className="btn-secondary kb-dialog-left" disabled={busy || naming !== null}
          onClick={() => { setNaming(""); setFailure(null); }}>
          新建文件夹
        </button>
        <button type="button" className="btn-secondary" disabled={busy} onClick={onClose}>取消</button>
        <button type="submit" className="btn" disabled={busy || view === null || dir === origin || naming !== null}
          title={dir === origin ? "资料已经在这个文件夹里" : undefined}>
          {busy && naming === null ? "移动中…" : "移到这里"}
        </button>
      </div>
    </KbDialog>
  );
}

export function DeleteDialog({ target, onDone, onClose }: {
  target: KbTarget;
  onDone: () => void;
  onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    try {
      await deleteKbDocument(target.path, target.version);
      onDone();
    } catch (error) {
      setFailure(failureText(error));
      setBusy(false);
    }
  };

  return (
    <KbDialog labelledBy="kb-delete-title" busy={busy} onClose={onClose} onSubmit={() => void submit()}>
      <h3 id="kb-delete-title" className="kb-dialog-title">删除资料？</h3>
      <p className="kb-dialog-text">
        《{target.title}》将从资料库移除，不再被检索到。历史版本仍保留，可以在对话里让 Agent 找回。
      </p>
      {failure !== null && <div className="kb-dialog-error" role="alert">{failure}</div>}
      <div className="kb-dialog-actions">
        <button type="button" className="btn-secondary" disabled={busy} onClick={onClose}>取消</button>
        <button type="submit" className="btn danger" disabled={busy} autoFocus>{busy ? "删除中…" : "删除"}</button>
      </div>
    </KbDialog>
  );
}

/** 页面上同一时间最多开一个资料对话框。 */
export type KbAction = { kind: "rename" | "move" | "delete"; target: KbTarget };
