import { expect, test } from "vitest";

import { kbAssetUrl, type KbListItem } from "./api";
import { breadcrumbs, countIn, displayTitle, folderExists, folderView, parentDir, relativePath } from "./kb";

const item = (path: string, title: string | null, updated_at: string | null = null): KbListItem => ({
  id: path, path, title, version: "v", updated_at,
});

const documents = [
  item("kb/项目/星云/验收.md", "验收纪要"),
  item("kb/inbox/b.md", "乙", "2026-09-02T00:00:00Z"),
  item("kb/项目/周会.md", "周会"),
  item("kb/inbox/a.md", "甲", "2026-09-10T00:00:00Z"),
  item("kb/说明.md", null),
];

test("每层只列直属文件夹与资料：文件夹按名称排并计入子文件夹资料，空文件夹也列出", () => {
  const root = folderView(documents, ["inbox", "项目", "项目/星云", "空的"], "");
  expect(root.folders).toEqual([
    { name: "空的", path: "空的", count: 0, updated_at: null },
    { name: "项目", path: "项目", count: 2, updated_at: null },
    { name: "inbox", path: "inbox", count: 2, updated_at: "2026-09-10T00:00:00Z" },
  ]);
  expect(root.documents.map(displayTitle)).toEqual(["说明"]);

  const project = folderView(documents, [], "项目");
  expect(project.folders).toEqual([{ name: "星云", path: "项目/星云", count: 1, updated_at: null }]);
  expect(project.documents.map(displayTitle)).toEqual(["周会"]);
});

test("资料最近更新在前", () => {
  expect(folderView(documents, [], "inbox").documents.map(displayTitle)).toEqual(["甲", "乙"]);
});

test("面包屑、上一层目录、文件夹是否存在与资料计数", () => {
  expect(breadcrumbs("")).toEqual([]);
  expect(breadcrumbs("项目/星云")).toEqual([
    { name: "项目", path: "项目" },
    { name: "星云", path: "项目/星云" },
  ]);
  expect(parentDir("kb/项目/星云/验收.md")).toBe("项目/星云");
  expect(parentDir("kb/说明.md")).toBe("");
  expect(folderExists(documents, [], "项目/星云")).toBe(true);
  expect(folderExists(documents, ["空的"], "空的")).toBe(true);
  expect(folderExists(documents, [], "不存在")).toBe(false);
  expect(countIn(documents, "项目")).toBe(2);
  expect(countIn(documents, "")).toBe(5);
});

test("路径去掉 kb/ 前缀", () => {
  expect(relativePath("kb/项目/验收.md")).toBe("项目/验收.md");
});

test("正文图片的 assets/ 路径换成读取接口地址，其他地址原样保留", () => {
  expect(kbAssetUrl("assets/3f2a9c0d1e7b4a56.png")).toBe("/api/kb/assets/3f2a9c0d1e7b4a56.png");
  expect(kbAssetUrl("https://example.com/a.png")).toBe("https://example.com/a.png");
  expect(kbAssetUrl("assets/sub/a.png")).toBe("assets/sub/a.png");
});
