import { expect, test } from "vitest";

import type { KbListItem } from "./api";
import { buildTree, displayTitle, parseTags, relativePath } from "./kb";

const item = (path: string, title: string | null): KbListItem => ({ id: path, path, title, tags: null, version: "v" });

test("资料按目录组织成树，目录在前、资料按标题排序", () => {
  const tree = buildTree([
    item("kb/项目/星云/验收.md", "验收纪要"),
    item("kb/inbox/b.md", "乙"),
    item("kb/项目/周会.md", "周会"),
    item("kb/inbox/a.md", "甲"),
    item("kb/说明.md", null),
  ]);

  expect(tree.folders.map((folder) => folder.name)).toEqual(["项目", "inbox"]);
  expect(tree.documents.map(displayTitle)).toEqual(["说明"]);
  const project = tree.folders[0];
  expect(project.path).toBe("项目");
  expect(project.folders.map((folder) => folder.path)).toEqual(["项目/星云"]);
  expect(project.documents.map(displayTitle)).toEqual(["周会"]);
  expect(tree.folders[1].documents.map(displayTitle)).toEqual(["甲", "乙"]);
});

test("标签按逗号、顿号或空白分隔并去重，路径去掉 kb/ 前缀", () => {
  expect(parseTags(" 课程，GSE、课程  实验 ,")).toEqual(["课程", "GSE", "实验"]);
  expect(parseTags("")).toEqual([]);
  expect(relativePath("kb/项目/验收.md")).toBe("项目/验收.md");
});
