/** 资料管理的纯函数：文件夹视图与路径的展示层换算，不写回后端。 */

import type { KbListItem } from "./api";

export type KbFolderEntry = {
  name: string;
  /** 资料库内的相对目录，不带 `kb/` 前缀。 */
  path: string;
  /** 文件夹里（含子文件夹）的资料数。 */
  count: number;
  /** 文件夹里（含子文件夹）最近一次资料更新时间；空文件夹为 null。 */
  updated_at: string | null;
};

export type KbFolderView = {
  folders: KbFolderEntry[];
  documents: KbListItem[];
};

/**
 * 某一层文件夹的内容：直属子文件夹与直属资料。
 *
 * 文件夹来自目录列表（含空文件夹），也从资料路径补出来，列表先后读取时不会漏掉；
 * 文件夹按名称排，资料最近更新在前。
 */
export function folderView(documents: KbListItem[], folders: string[], dir: string): KbFolderView {
  const prefix = dir ? `${dir}/` : "";
  const children = new Map<string, { count: number; updated_at: string | null }>();
  const direct: KbListItem[] = [];
  const addFolder = (path: string) => {
    if (!path.startsWith(prefix) || path === dir) return;
    const name = path.slice(prefix.length).split("/")[0];
    if (!children.has(name)) children.set(name, { count: 0, updated_at: null });
  };
  folders.forEach(addFolder);
  for (const document of documents) {
    const rel = relativePath(document.path);
    if (!rel.startsWith(prefix)) continue;
    const rest = rel.slice(prefix.length);
    const slash = rest.indexOf("/");
    if (slash === -1) {
      direct.push(document);
    } else {
      const name = rest.slice(0, slash);
      const entry = children.get(name) ?? { count: 0, updated_at: null };
      const updated = document.updated_at ?? null;
      children.set(name, {
        count: entry.count + 1,
        updated_at: updated !== null && (entry.updated_at === null || updated > entry.updated_at) ? updated : entry.updated_at,
      });
    }
  }
  return {
    folders: [...children.entries()]
      .map(([name, entry]) => ({ name, path: prefix + name, ...entry }))
      .sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN")),
    documents: direct.sort((a, b) =>
      (b.updated_at ?? "").localeCompare(a.updated_at ?? "")
        || displayTitle(a).localeCompare(displayTitle(b), "zh-Hans-CN")),
  };
}

/** 文件夹里（含子文件夹）的资料数；根目录为全部资料。 */
export function countIn(documents: KbListItem[], dir: string): number {
  const prefix = dir ? `${dir}/` : "";
  return documents.filter((document) => relativePath(document.path).startsWith(prefix)).length;
}

/** 当前文件夹是否存在：根目录、目录列表里有，或有资料在它下面。 */
export function folderExists(documents: KbListItem[], folders: string[], dir: string): boolean {
  return dir === "" || folders.includes(dir) || countIn(documents, dir) > 0;
}

/** 面包屑：从根到当前文件夹的每一层。 */
export function breadcrumbs(dir: string): { name: string; path: string }[] {
  if (!dir) return [];
  const parts = dir.split("/");
  return parts.map((name, index) => ({ name, path: parts.slice(0, index + 1).join("/") }));
}

/** 资料或文件夹所在的上一层目录（不带 `kb/` 前缀），根目录为空串。 */
export function parentDir(path: string): string {
  const parts = relativePath(path).split("/");
  parts.pop();
  return parts.join("/");
}

/** 地址里的文件夹参数：去掉首尾斜杠，根目录为空串。 */
export function normalizeDir(dir: string | null): string {
  return (dir ?? "").replace(/^\/+|\/+$/g, "");
}

export function folderLink(dir: string): string {
  return dir ? `/kb?${new URLSearchParams({ dir }).toString()}` : "/kb";
}

export function newDocumentLink(dir: string): string {
  return dir ? `/kb/new?${new URLSearchParams({ dir }).toString()}` : "/kb/new";
}

/** 资料库内的相对路径：去掉 `kb/` 前缀，界面上不重复展示。 */
export function relativePath(path: string): string {
  return path.startsWith("kb/") ? path.slice(3) : path;
}

export function fileName(path: string): string {
  return relativePath(path).split("/").pop() ?? path;
}

export function displayTitle(document: Pick<KbListItem, "title" | "path">): string {
  return document.title?.trim() || fileName(document.path).replace(/\.md$/, "");
}

export function documentLink(path: string): string {
  return `/kb/doc?${new URLSearchParams({ path }).toString()}`;
}
