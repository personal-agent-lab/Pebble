import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

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
  type SkillUsageRecord,
  type SkillVersion,
} from "../api";
import AppShell from "../components/AppShell";
import KbEditor from "../components/KbEditor";
import { MoreMenu } from "../components/MoreMenu";
import Notice from "../components/Notice";
import SkillFileTree, { SKILL_BODY } from "../components/SkillFileTree";
import { useNote } from "../hooks";
import { shortTime } from "../status";

/** 一次变更是谁做的（契约 §5）：管理页、用户在对话里要求的前台 Agent、后台复盘。
    注意它与 `origin` 不是一套取值——origin 说技能是谁创建的，这里说这次改动是谁发起的。 */
const ACTOR_LABELS: Record<string, string> = {
  user: "管理页",
  foreground: "对话里要求",
  review: "后台复盘",
};

const TABS: { state: SkillState; label: string }[] = [
  { state: "active", label: "启用中" },
  { state: "archived", label: "已归档" },
];

const BACK_ICON = <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"><polyline points="15 18 9 12 15 6" /></svg>;
const PANEL_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round">
    <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" />
  </svg>
);
const ARCHIVE_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 8h16v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z" /><path d="M4 4h16v4H4z" /><path d="M10 12h4" />
  </svg>
);
const RESTORE_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 12a8 8 0 1 0 8-8" /><polyline points="4 5 4 12 11 12" />
  </svg>
);

/** 窄屏放不下一整列文件树，默认收起来，由顶栏的按钮展开。 */
function wideEnough(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(min-width: 900px)").matches
    : true;
}

function isMarkdown(path: string): boolean {
  return /\.(md|markdown)$/i.test(path);
}

/** 来源说人话：契约 §2 的三个取值分别是谁创建的。 */
function originText(origin: SkillDetail["origin"]): string {
  if (origin === "user") return "由你手写";
  return origin === "explicit" ? "来自对话沉淀" : "来自后台复盘";
}

function asApiError(failure: unknown): ApiError {
  return failure instanceof ApiError ? failure : new ApiError("invalid_request", String(failure), 0);
}

function failureText(error: ApiError): string {
  const detail = error.fieldErrors?.map((item) => item.message).join("；");
  return detail ? detail : error.message;
}

/** 变更的目标技能：create 的 skill_id 在载荷里，其余在记录上。 */
function targetId(change: SkillChangeView): string | null {
  if (typeof change.payload.skill_id === "string" && change.payload.skill_id !== "") {
    return change.payload.skill_id;
  }
  return change.skill_id;
}

/** 动作的一行说明：说人话，不报字数——人关心改了什么，不是多长。 */
function actionLabel(change: SkillChangeView): string {
  if (change.action === "create") return "新建";
  if (change.action === "remove_file") return `删除附件 ${String(change.payload.relative_path ?? "")}`;
  if (change.action === "write_file") return `更新附件 ${String(change.payload.relative_path ?? "")}`;
  return typeof change.payload.body === "string" ? "替换正文" : "修改正文";
}

type DiffLine = { kind: "add" | "del" | "same"; text: string };

/** 行级 LCS diff：变更载荷最多几百行，O(n·m) 的表够用。 */
function lineDiff(oldText: string, newText: string): DiffLine[] {
  const a = oldText === "" ? [] : oldText.split("\n");
  const b = newText === "" ? [] : newText.split("\n");
  const lcs: number[][] = Array.from(
    { length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const lines: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { lines.push({ kind: "same", text: a[i] }); i++; j++; }
    else if (lcs[i + 1][j] >= lcs[i][j + 1]) { lines.push({ kind: "del", text: a[i] }); i++; }
    else { lines.push({ kind: "add", text: b[j] }); j++; }
  }
  for (; i < a.length; i++) lines.push({ kind: "del", text: a[i] });
  for (; j < b.length; j++) lines.push({ kind: "add", text: b[j] });
  return lines;
}

/**
 * 技能管理页：目录、创建与编辑、归档恢复、附件、历史版本与待审变更。
 *
 * 目录页与资料列表同一套行；点进一个技能后与资料页同一套骨架——顶栏是这个对象的操作，
 * 正文一栏到底，只是左边多一列文件树：一个技能是一个目录，正文之外还有 references/ 与
 * templates/ 两层附件（`skill-spec.md` §3），树把它们摆出来，右边读其中一份。
 */
export default function SkillsPage() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("id");
  const creating = params.get("new") === "1";
  const state = (params.get("state") as SkillState) ?? "active";
  const [entries, setEntries] = useState<SkillSummary[] | null>(null);
  const [changes, setChanges] = useState<SkillChangeView[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [note, setNote] = useNote();
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState<string | null>(null);

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

  // 换标签页时收起上一页的操作结果：提示只对做出动作的那一页有意义。
  useEffect(() => { setNote(null); setActionError(null); }, [state]);

  // 回目录时保留所在的标签页：从“已归档”点进去的技能，退回后还应该在那一页。
  const back = () => setParams(state === "active" ? {} : { state });

  /** 行尾 ⋯ 菜单的归档与恢复：做完刷新目录，结果一句话留在列表上方。 */
  const rowAction = async (entry: SkillSummary, action: () => Promise<unknown>, message: string) => {
    if (acting !== null) return;
    setActing(entry.skill_id);
    setActionError(null);
    setNote(null);
    try {
      await action();
      setNote(message);
      await load();
    } catch (failure) {
      setActionError(failureText(asApiError(failure)));
    } finally {
      setActing(null);
    }
  };

  if (creating || selected !== null) {
    return (
      <AppShell serviceError={error}>
        <SkillDocument key={selected ?? "new"} skillId={selected} creating={creating}
          onBack={back} onChanged={load} />
      </AppShell>
    );
  }

  return (
    <AppShell serviceError={error}>
      <div className="topbar">
        <div className="topbar-text">
          <h2>技能</h2>
        </div>
        <button type="button" className="btn-secondary" onClick={() => setParams({ new: "1" })}>新建技能</button>
      </div>
      <div className="content skills-content">
        {error !== null && (
          <Notice tone="danger" title="读取技能失败"
            actions={<button type="button" className="btn-secondary" onClick={() => void load()}>重试</button>}>
            {error.message}
          </Notice>
        )}

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

        {note !== null && <div className="kb-list-note" role="status">{note}</div>}
        {actionError !== null && <Notice tone="danger" title="操作失败">{actionError}</Notice>}

        {/* 待审变更排在目录前面：它是页面上唯一需要行动的东西，目录只是备查。 */}
        {changes !== null && changes.length > 0 && <ChangesPane changes={changes} onChanged={load} />}

        {entries === null && error === null && <div className="loading">读取中…</div>}
        {entries !== null && entries.length === 0 && (
          <div className="empty">
            <div className="empty-title">{state === "archived" ? "没有已归档的技能" : "还没有技能"}</div>
            <div className="empty-sub">手动新建，或在对话里让 Agent 把做法沉淀成技能。</div>
          </div>
        )}
        {entries !== null && entries.length > 0 && (
          // 与资料列表同一套行：名称、说明、定宽时间列，表头三列上下对齐。
          // 名称加一句描述就是每轮注入模型的技能目录，行里只留这两项与时间，不加标签；
          // 行尾的 ⋯ 与资料行同一套——启用中收“归档”，已归档收“恢复启用”。
          <div className="list">
            <div className="list-head" aria-hidden="true">
              <span>名称</span>
              <span className="list-time">最近更新</span>
              <span className="list-slot" />
            </div>
            {entries.map((entry) => (
              <div className="list-item" key={entry.skill_id}>
                <button type="button" className="list-link" onClick={() => setParams({ id: entry.skill_id })}>
                  <span className="list-title">{entry.name}</span>
                  {/* 说明与名称同一行，给模型判断相关性用；行里放不下时截断，全文在悬停提示与详情里。 */}
                  <span className="list-sub" title={entry.description}>{entry.description}</span>
                  <span className="list-time">{shortTime(entry.updated_at)}</span>
                </button>
                <MoreMenu items={state === "archived"
                  ? [{
                    key: "restore", label: "恢复启用", icon: RESTORE_ICON,
                    onSelect: () => void rowAction(entry, () => restoreSkill(entry.skill_id), "已恢复"),
                  }]
                  : [{
                    key: "archive", label: "归档", icon: ARCHIVE_ICON, danger: true,
                    onSelect: () => void rowAction(
                      entry,
                      () => archiveSkill(entry.skill_id),
                      "已归档",
                    ),
                  }]} />
              </div>
            ))}
          </div>
        )}
      </div>
    </AppShell>
  );
}

type DocumentProps = {
  /** 查看已有的技能；新建时为 null。 */
  skillId: string | null;
  creating: boolean;
  onBack: () => void;
  onChanged: () => void;
};

/**
 * 一个技能的页面：左边文件树，右边当前文件。
 *
 * 整页是一份草稿——名称、说明、正文、附件都在这里改，保存时一次提交，
 * 因此没有“编辑/阅读”两个状态，也不会有改了一半的附件悄悄写进技能目录。
 * 保存按顺序调用接口并带上每一次返回的 revision：正文与附件同属一个版本（契约 §2）。
 */
function SkillDocument({ skillId, creating, onBack, onChanged }: DocumentProps) {
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const [note, setNote] = useNote();
  const [busy, setBusy] = useState(false);
  const [treeOpen, setTreeOpen] = useState(wideEnough);

  const [skillKey, setSkillKey] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [current, setCurrent] = useState(SKILL_BODY);
  /** 已保存文件的原文，也是“改没改过”的比较基准。 */
  const [original, setOriginal] = useState<Record<string, string>>({ [SKILL_BODY]: "" });
  /** 读过一次的文件内容，切回去不再请求。 */
  const [loaded, setLoaded] = useState<Record<string, string>>({});
  /** 草稿里被改过的文件内容。 */
  const [files, setFiles] = useState<Record<string, string>>({});
  const [added, setAdded] = useState<string[]>([]);
  const [removed, setRemoved] = useState<string[]>([]);
  /** 编辑器里敲过字但还没等到内容变更通知的文件：保存按钮不等防抖。 */
  const [touched, setTouched] = useState<string[]>([]);
  const [editorKey, setEditorKey] = useState(0);

  const reader = useRef<(() => string) | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const uploadDir = useRef<string>("references/");

  const forget = useCallback(() => {
    setFiles({});
    setAdded([]);
    setRemoved([]);
    setTouched([]);
    setLoaded({});
    setEditorKey((key) => key + 1);
  }, []);

  const reload = useCallback(async () => {
    forget();
    if (creating || skillId === null) {
      setDetail(null);
      setSkillKey("");
      setName("");
      setDescription("");
      setOriginal({ [SKILL_BODY]: "" });
      setCurrent(SKILL_BODY);
      setLoadError(null);
      return;
    }
    try {
      const found = await getSkill(skillId);
      setDetail(found);
      setName(found.name);
      setDescription(found.description);
      setOriginal({ [SKILL_BODY]: found.body });
      setCurrent(SKILL_BODY);
      setLoadError(null);
    } catch (error) {
      setLoadError(asApiError(error));
    }
  }, [creating, skillId, forget]);

  useEffect(() => { void reload(); }, [reload]);

  /** 保存后重新取一份：换掉基准与 revision，但停在用户当前看的文件上。 */
  const refresh = async () => {
    if (skillId === null) return;
    const found = await getSkill(skillId);
    setDetail(found);
    setName(found.name);
    setDescription(found.description);
    setOriginal({ [SKILL_BODY]: found.body });
    forget();
    // 保存前在读哪份文件，保存后还读哪份；它要是刚被删掉，就退回正文。
    if (current === SKILL_BODY) return;
    if (found.files.some((file) => file.path === current)) await read(current);
    else setCurrent(SKILL_BODY);
  };

  /** 读一份附件：读回来之前不建编辑器，否则会先建出一个空文件。 */
  const read = async (path: string) => {
    if (skillId === null) return;
    try {
      const found = await readSkillFile(skillId, path);
      setLoaded((map) => ({ ...map, [path]: found.content }));
      setOriginal((map) => ({ ...map, [path]: found.content }));
    } catch (error) {
      setFailure(asApiError(error));
    }
  };

  /** 把当前编辑器里的内容收进草稿：编辑器的内容变更通知有防抖，切换与保存都要直接读它。 */
  const capture = (): Record<string, string> => {
    const latest = reader.current?.();
    return latest === undefined || latest === files[current]
      ? files
      : { ...files, [current]: latest };
  };

  // 渲染时只读草稿与基准，不去读编辑器：编辑器里的内容由内容变更通知同步进来，
  // 最后几个字可能还在防抖里，所以切换与保存那一刻另用 capture() 直接读一次。
  const textFor = (path: string) => files[path] ?? original[path] ?? loaded[path] ?? "";
  // 草稿里没有这个文件就是没改过：读过一次（original 里有值）不等于改过。
  const changed = (path: string) =>
    added.includes(path) || (files[path] !== undefined && files[path] !== original[path]);

  // 正文的内容随详情一起来（新建时是空的），附件要读一次；读到之前不建编辑器，别把它建成空文件。
  const pending = current !== SKILL_BODY && files[current] === undefined && loaded[current] === undefined;

  const attachments = Array.from(new Set([
    ...(detail?.files ?? []).map((file) => file.path),
    ...added,
  ])).sort();
  const visible = [SKILL_BODY, ...attachments];

  const dirty = creating
    ? skillKey.trim() !== "" || name.trim() !== "" || description.trim() !== "" || added.length > 0
      || textFor(SKILL_BODY).trim() !== ""
    : detail !== null && (
      name !== detail.name || description !== detail.description
      || textFor(SKILL_BODY) !== original[SKILL_BODY]
      || added.length > 0 || removed.length > 0
      || attachments.some(changed) || touched.length > 0
    );

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const leave = () => {
    if (dirty && !window.confirm("有未保存的修改，确定离开吗？")) return;
    onBack();
  };

  const open = (path: string) => {
    if (path === current) return;
    const drafted = capture();
    setFiles(drafted);
    setCurrent(path);
    if (path === SKILL_BODY || drafted[path] !== undefined || loaded[path] !== undefined) return;
    void read(path);
  };

  // “×”对已保存的附件是先标记、保存时才真正删除；对这次新加的附件是直接从草稿里去掉。
  const drop = (path: string) => {
    if (added.includes(path)) {
      setAdded((list) => list.filter((item) => item !== path));
      setFiles((map) => { const next = { ...map }; delete next[path]; return next; });
      setLoaded((map) => { const next = { ...map }; delete next[path]; return next; });
      if (current === path) setCurrent(SKILL_BODY);
      return;
    }
    setRemoved((list) => (list.includes(path) ? list.filter((item) => item !== path) : [...list, path]));
  };

  const upload = async (picked: FileList | null) => {
    if (picked === null || picked.length === 0) return;
    const next: Record<string, string> = {};
    for (const file of Array.from(picked)) next[`${uploadDir.current}${file.name}`] = await file.text();
    setFiles((map) => ({ ...map, ...next }));
    setAdded((list) => Array.from(new Set([...list, ...Object.keys(next)])));
    setRemoved((list) => list.filter((path) => !(path in next)));
    setCurrent(Object.keys(next)[0]);
  };

  const markTouched = (path: string) =>
    setTouched((list) => (list.includes(path) ? list : [...list, path]));

  const store = (path: string, markdown: string) => setFiles((map) => ({ ...map, [path]: markdown }));

  const act = async (action: () => Promise<unknown>, message: string, next: Partial<SkillDetail>) => {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    setNote(null);
    try {
      await action();
      setDetail((found) => (found === null ? found : { ...found, ...next }));
      onChanged();
      setNote(message);
    } catch (error) {
      setFailure(asApiError(error));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (busy) return;
    const drafted = capture();
    const body = drafted[SKILL_BODY] ?? original[SKILL_BODY] ?? "";
    const nextName = name.trim();
    const nextDescription = description.trim();
    if (nextName === "" || nextDescription === "" || body.trim() === "") return;
    setBusy(true);
    setFailure(null);
    setNote(null);
    try {
      if (creating) {
        await createSkill({
          skill_id: skillKey.trim(), name: nextName, description: nextDescription, body,
          attachments: added.length > 0
            ? Object.fromEntries(added.map((path) => [path, drafted[path] ?? ""]))
            : undefined,
        });
        onChanged();
        onBack();
        return;
      }
      if (detail === null || skillId === null) return;
      if (!dirty) {
        setNote("没有需要保存的改动");
        return;
      }
      let revision = detail.revision;
      if (body !== original[SKILL_BODY] || nextName !== detail.name || nextDescription !== detail.description) {
        revision = (await updateSkill(skillId, revision, {
          name: nextName, description: nextDescription, body,
        })).skill.revision;
      }
      for (const path of attachments) {
        if (!added.includes(path) && !changed(path)) continue;
        revision = (await writeSkillFile(skillId, path, drafted[path] ?? original[path] ?? "", revision)).skill.revision;
      }
      for (const path of removed) {
        revision = (await removeSkillFile(skillId, path, revision)).skill.revision;
      }
      await refresh();
      onChanged();
      setNote("已保存");
    } catch (error) {
      setFailure(asApiError(error));
    } finally {
      setBusy(false);
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

  const conflict = failure?.code === "skill_conflict";
  const archived = detail?.state === "archived";
  const ready = creating || detail !== null;
  const filled = name.trim() !== "" && description.trim() !== "" && textFor(SKILL_BODY).trim() !== ""
    && (!creating || skillKey.trim() !== "");

  if (!creating && (skillId === null || loadError !== null)) {
    return (
      <div className="content">
        <Notice tone="danger" title={loadError?.httpStatus === 404 ? "技能不存在" : "读取技能失败"}
          actions={<button type="button" className="btn-secondary" onClick={onBack}>返回技能列表</button>}>
          {loadError?.httpStatus === 404 ? "这个技能可能已被归档或改名。" : loadError?.message}
        </Notice>
      </div>
    );
  }

  return (
    <div className={`skill-detail${treeOpen ? " tree-open" : ""}`}>
      <aside className="skill-tree">
        <div className="skill-tree-head">
          <span>文件</span>
          <span className="skill-tree-count">{visible.length}</span>
        </div>
        {ready && (
          <SkillFileTree
            files={visible}
            dirty={touched}
            removed={removed}
            current={current}
            onSelect={open}
            onUpload={(dir) => { uploadDir.current = dir; fileInput.current?.click(); }}
            onRemove={drop}
          />
        )}
        <input ref={fileInput} type="file" multiple hidden aria-label="添加附件"
          onChange={(event) => { void upload(event.target.files); event.target.value = ""; }} />
      </aside>

      <div className="skill-doc">
        <div className="topbar">
          <button type="button" className={`icon-btn${treeOpen ? " on" : ""}`} aria-expanded={treeOpen}
            aria-label={treeOpen ? "收起文件列表" : "展开文件列表"} title="文件列表"
            onClick={() => setTreeOpen((open) => !open)}>
            {PANEL_ICON}
          </button>
          <button type="button" className="back" onClick={leave} aria-label="返回技能列表">{BACK_ICON}</button>
          <nav className="kb-doc-where" aria-label="所在位置">
            <Link to="/skills">技能</Link>
          </nav>
          {archived && <span className="kb-dirty">已归档，不进入对话</span>}
          {!archived && dirty && <span className="kb-dirty">未保存</span>}
          {note !== null && !dirty && !archived && <span className="kb-note" role="status">{note}</span>}
          <button type="button" className={dirty ? "btn" : "btn-secondary"} disabled={busy || !dirty || !filled}
            title={filled ? "保存（⌘S）" : "名称、说明与正文都不能为空"}
            onClick={() => void save()}>
            {busy ? "保存中…" : creating ? "创建" : "保存"}
          </button>
          {!creating && detail !== null && (
            <MoreMenu items={archived
              ? [{
                key: "restore", label: "恢复启用", icon: RESTORE_ICON,
                onSelect: () => void act(() => restoreSkill(detail.skill_id), "已恢复", { state: "active" }),
              }]
              : [{
                key: "archive", label: "归档", icon: ARCHIVE_ICON, danger: true,
                onSelect: () => void act(() => archiveSkill(detail.skill_id), "已归档", { state: "archived" }),
              }]} />
          )}
        </div>

        <div className="skill-doc-body">
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

          {!ready && <div className="loading">读取中…</div>}

          {/* SKILL.md 的头部就是它的 frontmatter：这些字段是给模型与程序读的，不是文档的开头段落，
              所以照文件里的样子摆在最开始，并标明它们是元数据。附件没有 frontmatter，只有一个路径。 */}
          {ready && current === SKILL_BODY && (
            <>
              <section className="skill-meta" aria-label="元数据">
                <span className="skill-meta-label">元数据</span>
                <div className="skill-meta-grid">
                  <span className="skill-meta-key">name</span>
                  <input className="skill-meta-input" value={name} placeholder="技能名称" aria-label="名称"
                    onChange={(event) => setName(event.target.value)} />
                  <span className="skill-meta-key">description</span>
                  <input className="skill-meta-input" value={description} maxLength={160} aria-label="一句话描述"
                    placeholder="一句话说明它做什么、什么时候用（每轮都会进上下文）"
                    onChange={(event) => setDescription(event.target.value)} />
                  <span className="skill-meta-key">skill_id</span>
                  {creating ? (
                    <input className="skill-meta-input skill-meta-code" value={skillKey} aria-label="标识"
                      placeholder="小写字母数字与连字符，如 weekly-report"
                      onChange={(event) => setSkillKey(event.target.value)} />
                  ) : (
                    <span className="skill-meta-value skill-meta-code">{detail?.skill_id}</span>
                  )}
                  {!creating && detail !== null && (
                    <>
                      <span className="skill-meta-key">origin</span>
                      <span className="skill-meta-value">{originText(detail.origin)}</span>
                    </>
                  )}
                </div>
                {creating && (
                  <p className="skill-meta-hint">skill_id 是目录名，创建后不改；Agent 按它引用这个技能。</p>
                )}
              </section>
              {/* 名称在元数据块里改，这里是它的呈现：正文之上仍然要有标题。
                  下面是正文，不再画分隔线——元数据块已经把头部与正文分开了。 */}
              {name.trim() !== "" && <h1 className="skill-title">{name}</h1>}
            </>
          )}

          {ready && current !== SKILL_BODY && (
            <div className="skill-file-head">
              <span className="skill-file-path">{current}</span>
              {removed.includes(current) && <span className="skill-file-flag">保存后删除</span>}
            </div>
          )}

          {ready && pending && <div className="loading">读取中…</div>}

          {ready && !pending && (
            isMarkdown(current) ? (
              <KbEditor
                key={`${current}:${editorKey}`}
                className={current === SKILL_BODY ? "skill-editor-body" : undefined}
                label={current === SKILL_BODY ? "技能正文" : `${current} 的内容`}
                initial={textFor(current)}
                onReady={(markdown) => setOriginal((map) => ({ ...map, [current]: markdown }))}
                onChange={(markdown) => store(current, markdown)}
                onInput={() => markTouched(current)}
                reader={reader}
              />
            ) : (
              <textarea
                key={`${current}:${editorKey}`}
                className="skill-file-text"
                aria-label={`${current} 的内容`}
                spellCheck={false}
                value={textFor(current)}
                onChange={(event) => { store(current, event.target.value); markTouched(current); }}
              />
            )
          )}

          {!creating && detail !== null && (
            <>
              {/* 技能在运行里的事实一行说完；它们不属于正文，所以跟在正文后面，不挡在读的路上。 */}
              <div className="kb-meta skill-facts">
                <span>更新于 {shortTime(detail.updated_at)}</span>
                <span>{recentLoads(detail.usage) > 0
                  ? `最近 7 天被使用 ${recentLoads(detail.usage)} 次`
                  : "最近 7 天未被使用"}</span>
                {detail.origin === "user" ? (
                  <span title="你手写的技能不会被自动改写">后台复盘只能提建议</span>
                ) : (
                  <label className="skill-managed"
                    title="勾选后后台复盘可以直接改写它；取消勾选则改为提出修改建议，等你确认">
                    <input type="checkbox" checked={detail.managed} disabled={busy}
                      onChange={(event) => void act(
                        () => setSkillManaged(detail.skill_id, event.target.checked),
                        event.target.checked ? "已允许复盘直接修改" : "已改为复盘需确认",
                        { managed: event.target.checked },
                      )} />
                    后台复盘可直接修改
                  </label>
                )}
              </div>

              <details className="skill-advanced">
                <summary>历史版本</summary>
                <div className="skill-advanced-body">
                  {/* 使用记录不再是单独一张表：哪一版被读过几次，写在对得上的那一版上。 */}
                  <SkillVersions skillId={detail.skill_id} revision={detail.revision} usage={detail.usage}
                    busy={busy} onFailure={setFailure}
                    onRestore={(revision) => void act(
                      () => restoreSkillVersion(detail.skill_id, revision),
                      `已恢复到 ${revision.slice(0, 8)}`,
                      {},
                    ).then(() => void refresh())} />
                </div>
              </details>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** 最近 7 天被装配的次数：加载只说明内容交给了模型，所以这里只报次数，不下结论。 */
function recentLoads(usage: SkillUsageRecord[]): number {
  const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1000;
  return usage.filter((record) => Date.parse(record.loaded_at) >= weekAgo).length;
}

type LoadStat = { count: number; last: string; lastSource: SkillUsageRecord["source"] };

/** 按版本汇总加载记录：哪一版被读过几次、最近一次什么时候、是自动读取还是你点名要的。 */
function loadsByRevision(usage: SkillUsageRecord[]): Map<string, LoadStat> {
  const stats = new Map<string, LoadStat>();
  for (const record of usage) {
    const found = stats.get(record.revision);
    if (found === undefined) {
      stats.set(record.revision, { count: 1, last: record.loaded_at, lastSource: record.source });
      continue;
    }
    found.count += 1;
    if (record.loaded_at > found.last) {
      found.last = record.loaded_at;
      found.lastSource = record.source;
    }
  }
  return stats;
}

/** 历史版本：查看某个版本的正文，或把它恢复成当前版本。
    每一版被读过几次写在这一行上——使用记录不再是单独一张表，它只有对着版本看才有意义。 */
function SkillVersions({ skillId, revision, usage, busy, onFailure, onRestore }: {
  skillId: string;
  revision: string;
  usage: SkillUsageRecord[];
  busy: boolean;
  onFailure: (error: ApiError) => void;
  onRestore: (revision: string) => void;
}) {
  const [versions, setVersions] = useState<SkillVersion[] | null>(null);
  const [viewing, setViewing] = useState<{ revision: string; body: string } | null>(null);
  const loads = loadsByRevision(usage);

  useEffect(() => {
    listSkillVersions(skillId).then(setVersions).catch(() => setVersions(null));
  }, [skillId, revision]);

  return (
    <div className="skill-versions">
      {versions === null && <span className="skill-attachments-empty">读取中…</span>}
      {versions !== null && versions.length === 0 && <span className="skill-attachments-empty">无</span>}
      {versions !== null && versions.map((version) => {
        const load = loads.get(version.revision);
        return (
          // 同一个 revision 可以对应多次提交（改启用状态、改复盘策略都不动正文），时间才是这一行的身份。
          <div className="skill-version-row" key={`${version.revision}:${version.created_at}`}>
            <span>{shortTime(version.created_at)}</span>
            {version.actor !== null && (
              <span className="skill-origin">{ACTOR_LABELS[version.actor] ?? version.actor}</span>
            )}
            {/* 启用状态与复盘策略的改动不产生内容变更，git 里也就没有变更说明。 */}
            <span className="skill-version-reason">{version.reason ?? "设置变更，正文没有变"}</span>
            {load !== undefined && (
              <span className="skill-version-loads"
                title={`最近一次${load.lastSource === "manual" ? "由你手动装配" : "由模型自动读取"}`}>
                被使用 {load.count} 次 · 最近 {shortTime(load.last)}
              </span>
            )}
            <span className="skill-revision" title={version.revision}>{version.revision.slice(0, 8)}</span>
            <button type="button" className="btn-secondary" disabled={busy}
              onClick={async () => {
                try {
                  setViewing({ revision: version.revision, body: (await getSkillVersion(skillId, version.revision)).body });
                } catch (error) {
                  onFailure(asApiError(error));
                }
              }}>查看</button>
            <button type="button" className="btn-secondary" disabled={busy || version.revision === revision}
              onClick={() => onRestore(version.revision)}>恢复</button>
          </div>
        );
      })}
      {viewing !== null && (
        <div className="skill-file-view">
          <div className="skill-file-view-head">
            <span>版本 {viewing.revision.slice(0, 8)} 的正文</span>
            <button type="button" onClick={() => setViewing(null)}>收起</button>
          </div>
          <pre>{viewing.body}</pre>
        </div>
      )}
    </div>
  );
}

/** 待审变更：复盘对用户手写技能（managed=false）只能提出建议，批准后才落盘。 */
function ChangesPane({ changes, onChanged }: { changes: SkillChangeView[]; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ id: string; error: ApiError } | null>(null);
  const [note, setNote] = useNote();
  // 目标技能当前的样子：名字给人看，正文给 diff 当基准。create 的技能还不存在，取不到就让它去。
  const [targets, setTargets] = useState<Record<string, { name: string; body: string }>>({});
  const idsKey = Array.from(
    new Set(changes.map(targetId).filter((id): id is string => id !== null)),
  ).join("\n");

  useEffect(() => {
    if (idsKey === "") return;
    let cancelled = false;
    void Promise.all(idsKey.split("\n").map(async (id) => {
      try {
        const found = await getSkill(id);
        return [id, { name: found.name, body: found.body }] as const;
      } catch {
        return [id, null] as const;
      }
    })).then((pairs) => {
      if (!cancelled) setTargets(Object.fromEntries(pairs.filter((pair) => pair[1] !== null)));
    });
    return () => { cancelled = true; };
  }, [idsKey]);

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
      <h3 id="skill-changes-title">
        待确认的技能变更{changes.length > 1 ? `（${changes.length}）` : ""}
      </h3>
      {note !== null && <div className="memory-note" role="status">{note}</div>}
      {changes.map((change) => {
        const id = targetId(change);
        return (
          <ChangeCard key={change.id} change={change} known={id === null ? undefined : targets[id]}
            failure={failure?.id === change.id ? failure.error : null} busy={busy !== null}
            onApprove={() => void act(
              change,
              () => approveSkillChange(change.id, change.base_revision ?? ""),
              "已应用",
            )}
            onReject={() => void act(change, () => rejectSkillChange(change.id), "已驳回")} />
        );
      })}
    </section>
  );
}

/** 一条待审变更。diff 的基准随动作取：patch 的整份替换拿当前正文，
    write_file 拿当前附件（读不到就是新文件），create 没有基准、整段都是新增。 */
function ChangeCard({ change, known, failure, busy, onApprove, onReject }: {
  change: SkillChangeView;
  known: { name: string; body: string } | undefined;
  failure: ApiError | null;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  const skillId = targetId(change);
  const relativePath = typeof change.payload.relative_path === "string"
    ? change.payload.relative_path
    : null;
  const [fileBase, setFileBase] = useState<string | null>(null);

  useEffect(() => {
    if (change.action !== "write_file" || relativePath === null || skillId === null) return;
    let cancelled = false;
    readSkillFile(skillId, relativePath)
      .then((found) => { if (!cancelled) setFileBase(found.content); })
      .catch(() => { if (!cancelled) setFileBase(""); });
    return () => { cancelled = true; };
  }, [change, relativePath, skillId]);

  const name = change.action === "create" && typeof change.payload.name === "string"
    && change.payload.name !== ""
    ? change.payload.name
    : known?.name ?? skillId ?? "新技能";

  const newText = change.action === "patch"
    ? (typeof change.payload.body === "string"
      ? change.payload.body
      : String(change.payload.new_string ?? ""))
    : change.action === "create"
      ? String(change.payload.body ?? "")
      : String(change.payload.content ?? "");
  const oldText = change.action === "patch"
    ? (typeof change.payload.body === "string" ? known?.body ?? null : String(change.payload.old_string ?? ""))
    : change.action === "write_file"
      ? fileBase
      : "";
  const lines = useMemo(
    () => (oldText === null ? null : lineDiff(oldText, newText)),
    [oldText, newText],
  );
  const adds = lines?.filter((line) => line.kind === "add").length ?? 0;
  const dels = lines?.filter((line) => line.kind === "del").length ?? 0;

  return (
    <div className="skill-change">
      <div className="skill-change-head">
        <strong>{name}</strong>
        <span className="skill-origin">{ACTOR_LABELS[change.actor] ?? change.actor}</span>
        <span className="skill-change-action">{actionLabel(change)}</span>
      </div>
      <p className="skill-change-reason">{change.reason}</p>
      {change.action !== "remove_file" && (lines === null || lines.length > 0) && (lines === null
        ? <details className="skill-change-details">
            <summary>查看建议正文</summary>
            <pre className="skill-change-body">{newText}</pre>
          </details>
        : <details className="skill-change-details">
            <summary>
              查看完整差异
              {(adds > 0 || dels > 0) && (
                <span className="skill-change-stat" aria-label={`新增 ${adds} 行，删除 ${dels} 行`}>
                  {adds > 0 && <span className="stat-add">+{adds}</span>}
                  {dels > 0 && <span className="stat-del">−{dels}</span>}
                </span>
              )}
            </summary>
            <DiffView lines={lines} />
          </details>)}
      {change.evidence_item_ids.length > 0 && (
        <div className="skill-change-evidence" title={change.evidence_item_ids.join("、")}>
          依据 {change.evidence_item_ids.length} 条任务记录
        </div>
      )}
      {failure !== null && (
        <div className="kb-dialog-error" role="alert">
          {failure.code === "skill_conflict"
            ? "技能在建议提出后被改过，版本已过期。"
            : failureText(failure)}
        </div>
      )}
      <div className="skill-pane-actions">
        <button type="button" className="btn-secondary" disabled={busy}
          onClick={onReject}>驳回</button>
        <button type="button" className="btn" disabled={busy}
          onClick={onApprove}>批准</button>
      </div>
    </div>
  );
}

/** diff 的渲染：成段的未变行只留前后各两行，中间折叠成一行说明，不再是一整面墙。 */
function DiffView({ lines }: { lines: DiffLine[] }) {
  const pieces: Array<{ kind: "line"; line: DiffLine } | { kind: "skip"; count: number }> = [];
  let unchanged: DiffLine[] = [];
  const flush = () => {
    if (unchanged.length > 8) {
      for (const line of unchanged.slice(0, 2)) pieces.push({ kind: "line", line });
      pieces.push({ kind: "skip", count: unchanged.length - 4 });
      for (const line of unchanged.slice(-2)) pieces.push({ kind: "line", line });
    } else {
      for (const line of unchanged) pieces.push({ kind: "line", line });
    }
    unchanged = [];
  };
  for (const line of lines) {
    if (line.kind === "same") unchanged.push(line);
    else { flush(); pieces.push({ kind: "line", line }); }
  }
  flush();
  return (
    <div className="skill-change-diff">
      {pieces.map((piece, index) => piece.kind === "skip" ? (
        <div key={index} className="skill-change-diff-skip">…… 未改动的 {piece.count} 行 ……</div>
      ) : (
        <div key={index} className={`skill-change-diff-line ${piece.line.kind}`}>
          <span className="skill-change-diff-mark" aria-hidden="true">
            {piece.line.kind === "add" ? "+" : piece.line.kind === "del" ? "−" : ""}
          </span>
          <span>{piece.line.text === "" ? "\u00A0" : piece.line.text}</span>
        </div>
      ))}
    </div>
  );
}
