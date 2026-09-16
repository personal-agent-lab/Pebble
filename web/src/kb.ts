/** 资料管理的纯函数：目录树、路径与标签的展示层换算，不写回后端。 */

import type { KbListItem } from "./api";

export type KbFolder = {
  name: string;
  /** 资料库内的相对目录，根目录为空串。 */
  path: string;
  folders: KbFolder[];
  documents: KbListItem[];
};

/** 把平铺的资料列表按目录组织成树：目录在前按名称排，资料按标题排。 */
export function buildTree(documents: KbListItem[]): KbFolder {
  const root: KbFolder = { name: "", path: "", folders: [], documents: [] };
  for (const document of documents) {
    const parts = relativePath(document.path).split("/");
    parts.pop();
    let folder = root;
    for (const part of parts) {
      let next = folder.folders.find((item) => item.name === part);
      if (next === undefined) {
        next = { name: part, path: folder.path ? `${folder.path}/${part}` : part, folders: [], documents: [] };
        folder.folders.push(next);
      }
      folder = next;
    }
    folder.documents.push(document);
  }
  sortFolder(root);
  return root;
}

function sortFolder(folder: KbFolder): void {
  folder.folders.sort((a, b) => a.name.localeCompare(b.name, "zh-Hans-CN"));
  folder.documents.sort((a, b) => displayTitle(a).localeCompare(displayTitle(b), "zh-Hans-CN"));
  folder.folders.forEach(sortFolder);
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

/** 标签输入按逗号、顿号或空白分隔，去掉空项与重复项。 */
export function parseTags(text: string): string[] {
  const tags = text.split(/[,，、\s]+/).map((tag) => tag.trim()).filter((tag) => tag !== "");
  return [...new Set(tags)];
}

export function formatTags(tags: string[]): string {
  return tags.join(", ");
}

export function documentLink(path: string): string {
  return `/kb/doc?${new URLSearchParams({ path }).toString()}`;
}
