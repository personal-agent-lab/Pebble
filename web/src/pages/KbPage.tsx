import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, listKbDocuments, searchKb, type KbHit, type KbListItem } from "../api";
import AppShell from "../components/AppShell";
import Notice from "../components/Notice";
import { buildTree, displayTitle, documentLink, fileName, relativePath, type KbFolder } from "../kb";

const SEARCH_DELAY_MS = 300;

const FOLDER_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
  </svg>
);

const DOC_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
    <polyline points="14 3 14 8 19 8" />
  </svg>
);

function DocumentRow({ document }: { document: KbListItem }) {
  return (
    <Link className="kb-row" to={documentLink(document.path)}>
      {DOC_ICON}
      <span className="kb-row-title">{displayTitle(document)}</span>
      {document.summary && <span className="kb-row-summary">{document.summary}</span>}
      <span className="kb-row-file">{fileName(document.path)}</span>
    </Link>
  );
}

/** 目录默认展开：资料库是个人规模，一眼看全比层层点开更快。 */
function Folder({ folder }: { folder: KbFolder }) {
  return (
    <details className="kb-folder" open>
      <summary className="kb-folder-name">
        {FOLDER_ICON}
        <span>{folder.name}</span>
      </summary>
      <div className="kb-folder-body">
        <FolderContent folder={folder} />
      </div>
    </details>
  );
}

function FolderContent({ folder }: { folder: KbFolder }) {
  return (
    <>
      {folder.folders.map((child) => <Folder key={child.path} folder={child} />)}
      {folder.documents.map((document) => <DocumentRow key={document.path} document={document} />)}
    </>
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
          <div className="kb-hit-path">{relativePath(hit.path)}</div>
        </Link>
      ))}
    </div>
  );
}

/**
 * 资料浏览与搜索。搜索词写在地址里，从资料页返回时结果还在；
 * 没有搜索词时按目录列出全部资料。
 */
export default function KbPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [text, setText] = useState(q);
  const [documents, setDocuments] = useState<KbListItem[] | null>(null);
  const [hits, setHits] = useState<KbHit[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(async () => {
    try {
      setDocuments((await listKbDocuments()).documents);
      setError(null);
    } catch (failure) {
      setError(failure instanceof ApiError ? failure : new ApiError("invalid_request", String(failure), 0));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    const trimmed = text.trim();
    const timer = window.setTimeout(() => {
      if (trimmed === q) return;
      setParams(trimmed ? { q: trimmed } : {}, { replace: true });
    }, SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [text, q, setParams]);

  useEffect(() => {
    if (q === "") {
      setHits(null);
      return;
    }
    let cancelled = false;
    searchKb(q)
      .then((result) => { if (!cancelled) { setHits(result.results); setError(null); } })
      .catch((failure) => {
        if (!cancelled) setError(failure instanceof ApiError ? failure : new ApiError("invalid_request", String(failure), 0));
      });
    return () => { cancelled = true; };
  }, [q]);

  const tree = documents === null ? null : buildTree(documents);

  return (
    <AppShell serviceError={error}>
      <div className="topbar">
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2>资料</h2>
          <div className="sub">{documents !== null && `${documents.length} 份资料`}</div>
        </div>
        <button type="button" className="btn" onClick={() => navigate("/kb/new")}>新建资料</button>
      </div>
      <div className="content kb-content">
        <input
          className="input kb-search"
          type="search"
          value={text}
          placeholder="搜索资料内容、标题或标签…"
          aria-label="搜索资料"
          onChange={(event) => setText(event.target.value)}
        />

        {error !== null && (
          <Notice tone="danger" title={error.unavailable ? "资料库未接入" : "读取资料失败"}
            actions={<button type="button" className="btn-secondary" onClick={() => void load()}>重试</button>}>
            {error.message}
          </Notice>
        )}

        {q !== "" && hits !== null && <SearchResults hits={hits} />}

        {q === "" && tree !== null && (
          documents?.length === 0
            ? <div className="empty">
                <div className="empty-title">还没有资料</div>
                <div className="empty-sub">新建一份，或在对话里让 Agent 帮你保存。</div>
              </div>
            : <div className="kb-tree list-card"><FolderContent folder={tree} /></div>
        )}

        {documents === null && error === null && <div className="loading">读取中…</div>}
      </div>
    </AppShell>
  );
}
