import { useState } from "react";

/** 正文固定是这个文件名，它是技能目录的入口（契约 §1）。 */
export const SKILL_BODY = "SKILL.md";

/** 附件只有两层：参考资料与模板，正文指到才读取（`skill-spec.md` §3）。 */
export const SKILL_LAYERS = [
  { dir: "references/", label: "参考资料", hint: "正文提到时才读取" },
  { dir: "templates/", label: "模板", hint: "照它套格式时才读取" },
] as const;

const CHEVRON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="9 6 15 12 9 18" />
  </svg>
);
const DOC_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><polyline points="14 3 14 8 19 8" />
  </svg>
);
const FOLDER_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
  </svg>
);
const PLUS_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M12 5v14M5 12h14" /></svg>
);
const CLOSE_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M6 6l12 12M18 6L6 18" /></svg>
);
const UNDO_ICON = (
  <svg className="i" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
    <path d="M4 10h9a5 5 0 0 1 0 10h-3" /><polyline points="8 6 4 10 8 14" />
  </svg>
);

type Props = {
  /** 树里要显示的全部文件：正文 + 已保存的附件 + 这次新加的附件。 */
  files: string[];
  /** 内容改过、还没保存的文件，行尾点一个小点。 */
  dirty: string[];
  /** 标为待删除的文件：仍然列出来，划掉，点一下可以撤销。 */
  removed: string[];
  current: string;
  onSelect: (path: string) => void;
  /** 往某一层添加附件。 */
  onUpload: (dir: string) => void;
  onRemove: (path: string) => void;
};

/**
 * 技能的文件树：正文在根，附件按 `references/`、`templates/` 两层摆开。
 *
 * 一个技能是一个目录而不是一份文件——目录常驻、正文按需、附件再按需，这三层是技能的形态，
 * 所以用资源管理器的方式把层次摆出来，而不是把附件压成一排标签。
 * 选中只决定右边读哪一份，不改变任何语义。
 */
export default function SkillFileTree({
  files, dirty, removed, current, onSelect, onUpload, onRemove,
}: Props) {
  const [folded, setFolded] = useState<string[]>([]);
  const toggle = (dir: string) =>
    setFolded((list) => (list.includes(dir) ? list.filter((item) => item !== dir) : [...list, dir]));

  const attachments = files.filter((path) => path !== SKILL_BODY);

  const fileRow = (path: string, name: string) => {
    const isRemoved = removed.includes(path);
    return (
      <div key={path} className={`skill-tree-row${current === path ? " active" : ""}${isRemoved ? " removed" : ""}`}>
        <span className="skill-tree-twist" />
        <button type="button" className="skill-tree-open" title={path}
          aria-current={current === path ? "true" : undefined} onClick={() => onSelect(path)}>
          {DOC_ICON}
          <span className="skill-tree-name">{name}</span>
          {dirty.includes(path) && !isRemoved && <span className="skill-tree-dot" title="未保存" />}
        </button>
        <button type="button" className="skill-tree-act"
          aria-label={isRemoved ? `撤销删除 ${path}` : `删除 ${path}`}
          title={isRemoved ? "撤销删除" : "从技能里删除这个文件"}
          onClick={() => onRemove(path)}>
          {isRemoved ? UNDO_ICON : CLOSE_ICON}
        </button>
      </div>
    );
  };

  return (
    <nav className="skill-tree-scroll" aria-label="技能文件">
      {fileRow(SKILL_BODY, "正文")}

      {SKILL_LAYERS.map((layer) => {
        const inLayer = attachments.filter((path) => path.startsWith(layer.dir));
        const open = !folded.includes(layer.dir);
        return (
          <div key={layer.dir}>
            <div className="skill-tree-row folder">
              <button type="button" className="skill-tree-open" title={`${layer.dir} · ${layer.hint}`}
                aria-expanded={open} onClick={() => toggle(layer.dir)}>
                <span className={`skill-tree-twist${open ? " open" : ""}`}>{CHEVRON}</span>
                {FOLDER_ICON}
                <span className="skill-tree-name">{layer.label}</span>
              </button>
              <button type="button" className="skill-tree-act" aria-label={`添加${layer.label}`}
                title={`添加${layer.label}`} onClick={() => onUpload(layer.dir)}>
                {PLUS_ICON}
              </button>
            </div>
            {open && inLayer.length === 0 && <div className="skill-tree-empty">暂无文件</div>}
            {open && inLayer.map((path) => fileRow(path, path.slice(layer.dir.length)))}
          </div>
        );
      })}
    </nav>
  );
}
